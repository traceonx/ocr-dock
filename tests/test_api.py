import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from ocr_dock.app import create_app
from ocr_dock.config import Settings, load_api_key
from ocr_dock.engine import Engine, OcrError

KEY = 'test-api-key-for-ocr-dock-123456'


def png(width=50, height=25):
    stream = BytesIO()
    Image.new('RGB', (width, height), 'white').save(stream, format='PNG')
    return stream.getvalue()


class FakeModel:
    def __init__(self):
        self.calls = []

    def classification_box(self, data, conf=0.25, iou=0.45):
        self.calls.append((conf, iou))
        return 'a7B', [{'char': 'a', 'conf': .92, 'box': [1., 2., 3., 4.]}]


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.tmp.name), api_key=KEY, rate_per_minute=0)
        self.model = FakeModel()
        self.client = TestClient(create_app(self.settings, lambda: self.model))
        self.client.__enter__()
        self.image = png()
        self.auth = {'X-API-Key': KEY}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def body(self, **extra):
        return {'image_base64': base64.b64encode(self.image).decode(), **extra}

    def test_base64_and_custom_thresholds(self):
        response = self.client.post('/ocr', json=self.body(conf=.6, iou=.4), headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result['ok'])
        self.assertEqual(result['text'], 'a7B')
        self.assertEqual(result['details'][0]['conf'], .92)
        self.assertEqual(self.model.calls, [(.6, .4)])
        self.assertGreaterEqual(result['elapsed_ms'], 0)
        self.assertEqual(result['request_id'], response.headers['x-request-id'])

    def test_data_url_multipart_raw_and_bearer(self):
        body = self.body()
        body['image_base64'] = 'data:image/png;base64,' + body['image_base64']
        self.assertEqual(self.client.post('/ocr', json=body, headers=self.auth).status_code, 200)
        response = self.client.post('/ocr', files={'file': ('sample.png', self.image, 'image/png')},
                                    data={'conf': '0.7'}, headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.model.calls[-1], (.7, .45))
        self.assertEqual(self.client.post('/ocr', content=self.image,
            headers={'Content-Type':'image/png', 'Authorization':'Bearer ' + KEY}).status_code,200)

    def test_auth_is_required_and_does_not_echo_secrets(self):
        for auth in [{}, {'X-API-Key':'incorrect'}, {'Authorization':'Bearer incorrect'}]:
            response = self.client.post('/ocr', json=self.body(), headers=auth)
            self.assertEqual(response.status_code, 401)
            self.assertNotIn(KEY, response.text)
            self.assertEqual(response.json()['error']['code'], 'unauthorized')
        self.assertEqual(self.model.calls, [])

    def test_invalid_payloads_images_and_parameters(self):
        cases = [({'image_base64':'not base64!!'}, 422),
                 ({'image_base64':base64.b64encode(b'not an image').decode()}, 422),
                 ({'image_base64':''}, 422), ({'image_base64':3}, 422),
                 (self.body(conf=1.5),422), (self.body(iou=0),422),
                 (self.body(url='https://example.com/image.png'),422)]
        for body, expected in cases:
            with self.subTest(body_type=list(body)):
                response = self.client.post('/ocr', json=body, headers=self.auth)
                self.assertEqual(response.status_code, expected, response.text)
                self.assertFalse(response.json()['ok'])
                self.assertNotIn('not base64!!',response.text)
        self.assertEqual(self.client.post('/ocr',content='{broken',headers={**self.auth,'Content-Type':'application/json'}).status_code,400)
        self.assertEqual(self.client.post('/ocr',content='text',headers={**self.auth,'Content-Type':'text/plain'}).status_code,415)
        self.assertEqual(self.client.post('/ocr',files={'wrong':('x.png',self.image)},headers=self.auth).status_code,422)
        self.assertEqual(self.client.post('/ocr',files=[('file',('a.png',self.image)),('file',('b.png',self.image))],headers=self.auth).status_code,400)

    def test_image_pixel_and_request_limits(self):
        settings = replace(self.settings, max_image_bytes=80, max_pixels=100)
        with TestClient(create_app(settings, lambda:self.model)) as client:
            response = client.post('/ocr',content=png(11,10),headers={**self.auth,'Content-Type':'image/png'})
            self.assertEqual(response.status_code,413,response.text)
            self.assertEqual(response.json()['error']['code'],'too_many_pixels')
            self.assertEqual(client.post('/ocr',content=b'a'*81,headers={**self.auth,'Content-Type':'image/png'}).status_code,413)
            response = client.post('/ocr',content=b'x'*(settings.max_body_bytes+1),headers={**self.auth,'Content-Type':'application/json'})
            self.assertEqual(response.status_code,413)
            # Actual streaming length is checked even without Content-Length.
            response = client.post('/ocr',content=iter([b'x'*40000,b'y'*40000]),headers={**self.auth,'Content-Type':'application/json'})
            self.assertEqual(response.status_code,413)

    def test_rate_limit_and_retry_after(self):
        with TestClient(create_app(replace(self.settings,rate_per_minute=1),lambda:self.model)) as client:
            self.assertEqual(client.post('/ocr',json=self.body(),headers=self.auth).status_code,200)
            response=client.post('/ocr',json=self.body(),headers=self.auth)
            self.assertEqual(response.status_code,429)
            self.assertEqual(response.json()['error']['code'],'rate_limited')
            self.assertGreater(int(response.headers['Retry-After']),0)

    def test_busy_requests_are_bounded(self):
        entered, release = threading.Event(), threading.Event()
        model=self.model
        def blocking(data,conf=.25,iou=.45):
            entered.set()
            release.wait(5)
            return 'a7B',[]
        model.classification_box=blocking
        settings=replace(self.settings,max_queue=0)
        with TestClient(create_app(settings,lambda:model)) as client, ThreadPoolExecutor(1) as pool:
            first=pool.submit(client.post,'/ocr',json=self.body(),headers=self.auth)
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(client.get('/health').json()['pending'],1)
                second=client.post('/ocr',json=self.body(),headers=self.auth)
                self.assertEqual(second.status_code,429)
                self.assertEqual(second.json()['error']['code'],'server_busy')
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5).status_code,200)

    def test_health_and_documentation(self):
        result=self.client.get('/health')
        self.assertEqual(result.status_code,200)
        self.assertTrue(result.json()['ready'])
        self.assertEqual(result.json()['engine_version'],'2.2')
        self.assertNotIn(KEY,result.text)
        self.assertEqual(self.client.get('/docs').status_code,200)
        schema=self.client.get('/openapi.json').json()
        self.assertIn('multipart/form-data',schema['paths']['/ocr']['post']['requestBody']['content'])
        self.assertTrue(schema['paths']['/ocr']['post']['security'])

    def test_model_load_failure_is_reported(self):
        def fail(): raise RuntimeError('private model path')
        with self.assertLogs('ocr_dock',level='ERROR'):
            with TestClient(create_app(self.settings,fail)) as client:
                self.assertEqual(client.get('/health').status_code,503)
                response=client.post('/ocr',json=self.body(),headers=self.auth)
                self.assertEqual(response.status_code,503)
                self.assertNotIn('private model path',response.text)

    def test_cors_explicit_origins_only(self):
        settings=replace(self.settings,cors_origins=('https://client.example',))
        with TestClient(create_app(settings,lambda:self.model)) as client:
            response=client.options('/ocr',headers={'Origin':'https://client.example','Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'X-API-Key'})
            self.assertEqual(response.headers['access-control-allow-origin'],'https://client.example')
            denied=client.options('/ocr',headers={'Origin':'https://untrusted.example','Access-Control-Request-Method':'POST'})
            self.assertNotIn('access-control-allow-origin',denied.headers)


class KeyTests(unittest.TestCase):
    def test_generated_key_is_persistent_and_private(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(data_dir=Path(folder))
            key=load_api_key(settings)
            self.assertGreaterEqual(len(key),32)
            self.assertEqual(key,load_api_key(settings))
            self.assertEqual((Path(folder)/'api_key').stat().st_mode&0o777,0o600)
            self.assertEqual(load_api_key(replace(settings,api_key=KEY)),KEY)

    def test_invalid_keys_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with self.assertRaises(ValueError):load_api_key(Settings(data_dir=root,api_key='short'))
            outside=root/'outside';outside.write_text(KEY)
            (root/'api_key').symlink_to(outside)
            with self.assertRaises(OSError):load_api_key(Settings(data_dir=root))


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_client_keeps_inference_slot(self):
        started,release=threading.Event(),threading.Event()
        class Blocking:
            def classification_box(self,*args,**kwargs):
                started.set();release.wait(5);return 'x',[]
        engine=Engine(Settings(concurrency=1,max_queue=0),lambda:Blocking())
        await engine.start()
        task=asyncio.create_task(engine.recognize(png(),.25,.45))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait,3))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual(engine.pending,1)
            with self.assertRaises(OcrError) as caught:await engine.recognize(png(),.25,.45)
            self.assertEqual(caught.exception.code,'server_busy')
        finally:
            release.set();await engine.stop()
        self.assertEqual(engine.pending,0)


if __name__=='__main__':unittest.main()
