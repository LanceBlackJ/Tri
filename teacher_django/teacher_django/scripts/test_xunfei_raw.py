import os
import requests
import sys

api_url = os.getenv('XUNFEI_API_URL') or os.getenv('XINGHUO_API_URL')
api_key_present = bool(os.getenv('XUNFEI_API_KEY') or os.getenv('XINGHUO_API_KEY'))
model = os.getenv('XUNFEI_MODEL') or os.getenv('XINGHUO_DEFAULT_MODEL') or 'lite'
print('api_url=', api_url)
print('model=', model)
print('api_key_present=', api_key_present)

if not api_url:
    print('no api_url configured')
    sys.exit(1)

headers = {'Content-Type': 'application/json'}
api_key = os.getenv('XUNFEI_API_KEY') or os.getenv('XINGHUO_API_KEY')
if api_key:
    headers['Authorization'] = api_key

payload = {
    'model': model,
    'messages': [{'role': 'user', 'content': '测试'}],
    'temperature': 0.2,
    'max_tokens': 32
}

try:
    resp = requests.post(api_url, headers=headers, json=payload, timeout=10)
    print('status_code=', resp.status_code)
    print('response_text=')
    print(resp.text)
except Exception as e:
    print('request error:', e)
    sys.exit(2)
