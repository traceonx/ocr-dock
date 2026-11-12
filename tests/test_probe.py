from io import BytesIO
import unittest
import urllib.error
from tools.check_container import wait_ready


class ProbeTests(unittest.TestCase):
    def test_startup_resets_and_unready_health_are_retried(self):
        events=[ConnectionResetError('starting'),TimeoutError('starting'),
                urllib.error.URLError('connection refused'),
                BytesIO(b'{"ready":false}'),BytesIO(b'{"ready":true,"engine":"ppllocr"}')]
        def opener(*args,**kwargs):
            item=events.pop(0)
            if isinstance(item,Exception):raise item
            return item
        result=wait_ready('http://127.0.0.1',timeout=2,interval=0,opener=opener,sleep=lambda _:None)
        self.assertTrue(result['ready'])
        self.assertEqual(events,[])

    def test_probe_has_a_deadline(self):
        with self.assertRaisesRegex(RuntimeError,'did not become ready'):
            wait_ready('http://127.0.0.1',timeout=0)
