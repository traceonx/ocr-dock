from __future__ import annotations

import base64
import binascii
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
import hmac
import json
import logging
import os
from pathlib import Path
import time
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

from .config import Settings, load_api_key
from .engine import Engine, OcrError

log = logging.getLogger('ocr_dock')
ROOT = Path(__file__).resolve().parents[1]


class Options(BaseModel):
    conf: float = Field(default=0.25, ge=0.05, le=1)
    iou: float = Field(default=0.45, ge=0.05, le=1)
    model_config = ConfigDict(extra='forbid')


class Base64Input(Options):
    image_base64: StrictStr = Field(min_length=1)


class RateLimiter:
    def __init__(self, limit):
        self.limit = limit
        self.clients = OrderedDict()

    def check(self, peer):
        if not self.limit:
            return
        now = time.monotonic()
        history = self.clients.pop(peer, deque())
        while history and history[0] <= now - 60:
            history.popleft()
        self.clients[peer] = history
        while len(self.clients) > 1024:
            self.clients.popitem(last=False)
        if len(history) >= self.limit:
            retry = max(1, int(60 - (now - history[0])) + 1)
            raise OcrError(429, 'rate_limited', '请求过于频繁，请稍后重试', retry)
        history.append(now)


async def read_limited(request, limit):
    length = request.headers.get('content-length')
    if length is not None:
        if not length.isdecimal():
            raise OcrError(400, 'invalid_content_length', 'Content-Length 无效')
        if int(length) > limit:
            raise OcrError(413, 'request_too_large', '请求体超过大小限制')
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise OcrError(413, 'request_too_large', '请求体超过大小限制')
        chunks.append(chunk)
    request._body = b''.join(chunks)
    return request._body


def invalid_parameters(exc):
    # Never reflect Base64 images, credentials, or raw request bodies into errors.
    names = ', '.join('.'.join(str(p) for p in error['loc']) for error in exc.errors())
    return OcrError(422, 'invalid_parameters', '请求参数无效：' + names)


async def parse_image(request, settings):
    body = await read_limited(request, settings.max_body_bytes)
    mime = request.headers.get('content-type', '').split(';')[0].strip().lower()
    if mime == 'application/json':
        try:
            value = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise OcrError(400, 'invalid_json', '请求体必须是有效 JSON') from None
        try:
            payload = Base64Input.model_validate(value)
        except ValidationError as exc:
            raise invalid_parameters(exc) from None
        encoded = payload.image_base64
        if encoded.startswith('data:'):
            prefix, separator, encoded = encoded.partition(',')
            if not separator or not prefix.startswith('data:image/') or not prefix.endswith(';base64'):
                raise OcrError(422, 'invalid_base64', '图片 Data URL 格式无效')
        if len(encoded) > ((settings.max_image_bytes + 2) // 3) * 4:
            raise OcrError(413, 'image_too_large', '图片超过大小限制')
        try:
            image = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise OcrError(422, 'invalid_base64', '图片 Base64 格式无效') from None
        return image, Options(conf=payload.conf, iou=payload.iou)
    if mime == 'multipart/form-data':
        async with request.form(max_files=1, max_fields=2, max_part_size=settings.max_body_bytes) as form:
            if set(form) - {'file', 'conf', 'iou'}:
                raise OcrError(422, 'invalid_parameters', '表单仅支持 file、conf、iou')
            upload = form.get('file')
            if not isinstance(upload, UploadFile):
                raise OcrError(422, 'missing_file', '请在 file 字段上传图片')
            try:
                options = Options.model_validate({k: form[k] for k in ('conf', 'iou') if k in form})
            except ValidationError as exc:
                raise invalid_parameters(exc) from None
            image = await upload.read(settings.max_image_bytes + 1)
            return image, options
    if mime in {'image/png', 'image/jpeg', 'image/bmp', 'image/webp', 'application/octet-stream'}:
        return body, Options()
    raise OcrError(415, 'unsupported_media_type', '请使用 JSON、图片文件表单或图片二进制请求')


REQUEST_SCHEMA = {
    'requestBody': {
        'required': True,
        'content': {
            'application/json': {'schema': Base64Input.model_json_schema()},
            'multipart/form-data': {'schema': {
                'type': 'object', 'required': ['file'],
                'properties': {'file': {'type': 'string', 'format': 'binary'},
                               'conf': {'type': 'number', 'default': 0.25, 'minimum': 0.05, 'maximum': 1},
                               'iou': {'type': 'number', 'default': 0.45, 'minimum': 0.05, 'maximum': 1}}}},
            'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}},
        },
    }
}


def create_app(settings=None, model_loader=None):
    settings = settings or Settings.from_env()
    rate = RateLimiter(settings.rate_per_minute)

    @asynccontextmanager
    async def lifespan(app):
        app.state.api_key = load_api_key(settings)
        app.state.engine = Engine(settings, model_loader)
        await app.state.engine.start()
        log.info('OCR Dock initialized; API key source: %s', 'environment' if settings.api_key else 'data/api_key')
        try:
            yield
        finally:
            await app.state.engine.stop()

    version = (ROOT / 'VERSION').read_text().strip()
    app = FastAPI(title='OCR Dock', version=version,
                  description='ppllocr 验证码识别服务，支持图片、Base64 与字符置信度。', lifespan=lifespan)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                           allow_methods=['POST', 'GET'],
                           allow_headers=['X-API-Key', 'Authorization', 'Content-Type'])

    @app.middleware('http')
    async def metadata(request, call_next):
        request.state.request_id = uuid4().hex
        response = await call_next(request)
        response.headers['X-Request-ID'] = request.state.request_id
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'no-store'
        return response

    def failure(request, status, code, message, headers=None):
        return JSONResponse({'ok': False, 'error': {'code': code, 'message': message},
                             'request_id': getattr(request.state, 'request_id', '')},
                            status_code=status, headers=headers)

    @app.exception_handler(OcrError)
    async def ocr_error(request, exc):
        headers = {'Retry-After': str(exc.retry_after)} if exc.retry_after else {}
        return failure(request, exc.status, exc.code, exc.message, headers)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        message = str(exc.detail) if exc.status_code < 500 else '服务内部错误'
        return failure(request, exc.status_code, 'http_error', message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return failure(request, 422, 'invalid_parameters', invalid_parameters(exc).message)

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        log.error('Request %s failed (%s)', getattr(request.state, 'request_id', ''), type(exc).__name__)
        return failure(request, 500, 'internal_error', '识别失败，请检查服务日志')

    key_header = APIKeyHeader(name='X-API-Key', auto_error=False)
    bearer_header = HTTPBearer(auto_error=False)

    async def authenticate(request: Request,
                           key: str | None = Depends(key_header),
                           bearer: HTTPAuthorizationCredentials | None = Depends(bearer_header)):
        expected = request.app.state.api_key.encode()
        candidates = [key, bearer.credentials if bearer else None]
        if not any(hmac.compare_digest(candidate.encode(), expected) for candidate in candidates if candidate):
            raise OcrError(401, 'unauthorized', 'API Key 无效或缺失')
        rate.check(request.client.host if request.client else 'unknown')

    @app.get('/', include_in_schema=False)
    async def index():
        return {'name': 'OCR Dock', 'version': version, 'docs': '/docs', 'health': '/health'}

    @app.get('/health', summary='健康检查与模型就绪状态')
    async def health(request: Request):
        engine = request.app.state.engine
        ready = engine.model is not None
        return JSONResponse({'ok': ready, 'ready': ready, 'service': 'ocr-dock', 'version': version,
                             'engine': 'ppllocr', 'engine_version': '2.2', 'device': 'cpu',
                             'pending': engine.pending, 'error': engine.error,
                             'commit': os.environ.get('GIT_SHA', 'dev')[:12],
                             'build_time': os.environ.get('BUILD_TIME', '')},
                            status_code=200 if ready else 503)

    @app.post('/ocr', dependencies=[Depends(authenticate)], openapi_extra=REQUEST_SCHEMA,
              summary='识别验证码图片', responses={401: {'description': 'API Key 无效'},
                  413: {'description': '图片或请求过大'}, 415: {'description': '格式不支持'},
                  422: {'description': '图片或参数无效'}, 429: {'description': '限流或队列已满'},
                  503: {'description': '模型尚未就绪'}})
    async def recognize(request: Request):
        started = time.perf_counter()
        data, options = await parse_image(request, settings)
        text, details = await request.app.state.engine.recognize(data, options.conf, options.iou)
        return {'ok': True, 'text': text, 'details': details,
                'elapsed_ms': round((time.perf_counter() - started) * 1000, 2),
                'request_id': request.state.request_id}

    return app


app = create_app()
