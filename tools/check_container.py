"""Check a running container against real fixture images; no secrets are printed."""
import base64
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

base=(sys.argv[1] if len(sys.argv)>1 else 'http://127.0.0.1:18083').rstrip('/')
key=os.environ['OCR_API_KEY']
health=None
for _ in range(90):
    try:
        with urllib.request.urlopen(base+'/health',timeout=2) as response:
            health=json.load(response)
        if health.get('ready'):break
    except (urllib.error.URLError,TimeoutError):
        pass
    time.sleep(1)
else:
    raise SystemExit('OCR container did not become ready')
assert health['engine']=='ppllocr' and health['device']=='cpu',health
fixtures=Path(__file__).resolve().parents[1]/'tests/fixtures'
for name,expected in [('digits-49348.png','49348'),('mixed-k7Mz.png','k7Mz')]:
    payload=json.dumps({'image_base64':base64.b64encode((fixtures/name).read_bytes()).decode()}).encode()
    request=urllib.request.Request(base+'/ocr',data=payload,
        headers={'Content-Type':'application/json','X-API-Key':key})
    with urllib.request.urlopen(request,timeout=30) as response:
        result=json.load(response)
    assert result['text']==expected,result
    print(json.dumps({'fixture':name,'text':result['text'],'elapsed_ms':result['elapsed_ms']},ensure_ascii=False))
try:
    urllib.request.urlopen(urllib.request.Request(base+'/ocr',data=b'{}',
        headers={'Content-Type':'application/json'}),timeout=5)
except urllib.error.HTTPError as error:
    assert error.code==401
else:
    raise AssertionError('Unauthenticated request was accepted')
print('Container health, real OCR and API-key checks passed.')
