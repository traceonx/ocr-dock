from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import asyncio
from functools import partial
import logging

from PIL import Image, UnidentifiedImageError

log = logging.getLogger('ocr_dock')


class OcrError(Exception):
    def __init__(self, status, code, message, retry_after=None):
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


def load_model(threads):
    """Use upstream inference with a bounded CPU-only ONNX session."""
    import onnxruntime as ort
    import ppllocr.inference as upstream

    ort.disable_telemetry_events()
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    model_path = Path(upstream.__file__).parent / 'assets/ppllocr_betav2.2.onnx'
    # The inference/preprocessing/NMS methods are those of the pinned Ppllocr 2.2.
    # Configure the same model without the upstream constructor's unbounded thread defaults.
    model = upstream.OCR.__new__(upstream.OCR)
    model.class_names = upstream.CHARACTERS
    model.session = ort.InferenceSession(str(model_path), sess_options=options,
                                         providers=['CPUExecutionProvider'])
    model.input_name = model.session.get_inputs()[0].name
    model.output_name = model.session.get_outputs()[0].name
    model.img_size = (512, 512)
    return model


def validate_image(data, settings):
    if not data:
        raise OcrError(422, 'invalid_image', '图片不能为空')
    if len(data) > settings.max_image_bytes:
        raise OcrError(413, 'image_too_large', '图片超过大小限制')
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in {'PNG', 'JPEG', 'BMP', 'WEBP'}:
                raise OcrError(415, 'unsupported_image', '仅支持 PNG、JPEG、BMP、WebP')
            if image.width * image.height > settings.max_pixels:
                raise OcrError(413, 'too_many_pixels', '图片像素数量超过限制')
            if getattr(image, 'n_frames', 1) != 1:
                raise OcrError(415, 'animated_image', '请上传静态图片')
            image.load()
    except OcrError:
        raise
    except Image.DecompressionBombError:
        raise OcrError(413, 'too_many_pixels', '图片像素数量超过限制') from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise OcrError(422, 'invalid_image', '图片损坏或格式无法识别') from None


class Engine:
    def __init__(self, settings, loader=None):
        self.settings = settings
        self.loader = loader or partial(load_model, settings.ort_threads)
        self.model = None
        self.error = None
        self.pool = ThreadPoolExecutor(max_workers=settings.concurrency, thread_name_prefix='ocr')
        self.semaphore = asyncio.Semaphore(settings.concurrency)
        self.pending = 0
        self.tasks = set()

    async def start(self):
        try:
            self.model = await asyncio.get_running_loop().run_in_executor(self.pool, self.loader)
            if self.model is None:
                raise RuntimeError('Model loader returned no model')
        except Exception:
            self.error = 'model_load_failed'
            log.exception('Failed to load OCR model')

    def predict(self, data, conf, iou):
        validate_image(data, self.settings)
        text, details = self.model.classification_box(data, conf=conf, iou=iou)
        return text, details

    async def recognize(self, data, conf, iou):
        if self.model is None:
            raise OcrError(503, 'model_not_ready', 'OCR 模型未就绪')
        if self.pending >= self.settings.concurrency + self.settings.max_queue:
            raise OcrError(429, 'server_busy', '识别队列已满，请稍后重试', 1)
        self.pending += 1
        task = asyncio.create_task(self._execute(data, conf, iou))
        self.tasks.add(task)
        task.add_done_callback(self._finished)
        # A disconnected client must not release a slot while its CPU inference still runs.
        return await asyncio.shield(task)

    def _finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled():
            task.exception()  # Retrieve failures even if the original client disconnected.

    async def _execute(self, data, conf, iou):
        acquired = False
        try:
            try:
                await asyncio.wait_for(self.semaphore.acquire(), self.settings.queue_wait_seconds)
            except TimeoutError:
                raise OcrError(429, 'queue_timeout', '等待识别超时，请稍后重试', 1) from None
            acquired = True
            return await asyncio.get_running_loop().run_in_executor(
                self.pool, partial(self.predict, data, conf, iou))
        finally:
            if acquired:
                self.semaphore.release()
            self.pending -= 1

    async def stop(self):
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        self.pool.shutdown(wait=True)
