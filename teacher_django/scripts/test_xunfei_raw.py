import os
import requests
import sys

api_url = os.getenv('XUNFEI_API_URL') or os.getenv('XINGHUO_API_URL')
api_key_present = bool(os.getenv('XUNFEI_API_KEY') or os.getenv('XINGHUO_API_KEY'))
model = os.getenv('XUNFEI_MODEL') or os.getenv('XINGHUO_DEFAULT_MODEL') or 'lite'

# 如果环境变量未设置，尝试从项目根的 .env 文件读取（便于直接运行脚本）
if not api_url or not api_key_present:
    env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
    env_path = os.path.abspath(env_path)
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip()
                if k in ('XUNFEI_API_KEY', 'XINGHUO_API_KEY') and not os.getenv(k):
                    os.environ[k] = v
                if k in ('XUNFEI_API_URL', 'XINGHUO_API_URL') and not os.getenv(k):
                    os.environ[k] = v
                if k in ('XUNFEI_MODEL', 'XINGHUO_DEFAULT_MODEL') and not os.getenv(k):
                    os.environ[k] = v
    # refresh
    api_url = os.getenv('XUNFEI_API_URL') or os.getenv('XINGHUO_API_URL')
    api_key_present = bool(os.getenv('XUNFEI_API_KEY') or os.getenv('XINGHUO_API_KEY'))
    model = os.getenv('XUNFEI_MODEL') or os.getenv('XINGHUO_DEFAULT_MODEL') or model
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
