"""OCR_API_KEY=... python examples/client.py captcha.png http://NAS-IP:8083"""
import base64
import json
import os
from pathlib import Path
import sys
import urllib.request

image=Path(sys.argv[1])
url=(sys.argv[2] if len(sys.argv)>2 else 'http://127.0.0.1:8083').rstrip('/')+'/ocr'
request=urllib.request.Request(url,
    data=json.dumps({'image_base64':base64.b64encode(image.read_bytes()).decode()}).encode(),
    headers={'Content-Type':'application/json','X-API-Key':os.environ['OCR_API_KEY']})
with urllib.request.urlopen(request,timeout=30) as response:
    print(json.dumps(json.load(response),ensure_ascii=False,indent=2))
