import json
import os
import requests
from django.conf import settings


class XunfeiSparkClient:
    """
    讯飞星火大模型 REST API 客户端（基于您提供的 .env.local 配置）
    """
    
    def __init__(self):
        # 从环境变量获取配置（使用您提供的格式）
        # 优先从 Django settings 中读取（兼容 XINGHUO_* 与 XUNFEI_* 命名），再回退到环境变量
        self.api_key = getattr(settings, 'XINGHUO_API_KEY', '') or getattr(settings, 'XUNFEI_API_KEY', '') or os.getenv('XUNFEI_API_KEY', '')
        self.api_url = getattr(settings, 'XINGHUO_API_URL', '') or getattr(settings, 'XUNFEI_API_URL', '') or os.getenv('XUNFEI_API_URL', 'https://spark-api-open.xf-yun.com/v1/chat/completions')
        self.model = getattr(settings, 'XINGHUO_DEFAULT_MODEL', '') or getattr(settings, 'XUNFEI_MODEL', '') or os.getenv('XUNFEI_MODEL', 'lite')
        # 超时从配置读取：本地 CPU 模型单次调用可能很久，需要远大于 30s
        self.timeout = int(getattr(settings, 'XINGHUO_TIMEOUT', 30) or 30)

        if not self.api_key:
            raise ValueError("请在 .env 文件或 settings 中配置 XUNFEI_API_KEY / XINGHUO_API_KEY")

    def _proxies(self):
        # 只有外部讯飞星火云端点才需要走系统代理；本地或自建 Ollama（含远程 GPU 机器）
        # 一律绕过系统代理，否则会被代理拦成 502/超时。
        u = (self.api_url or '').lower()
        if 'xf-yun.com' in u or 'xfyun.cn' in u:
            return None  # 星火：走系统代理
        return {'http': None, 'https': None}  # Ollama（本地/远程 GPU）：绕过代理
    
    def get_response(self, messages, temperature=0.5):
        """
        获取 AI 响应（REST API 版本）
        messages: 对话历史列表，格式 [{"role": "user", "content": "..."}, ...]
        """
        headers = {
            'Authorization': self.api_key,
            'Content-Type': 'application/json'
        }
        
        data = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 2048
        }
        
        try:
            response = requests.post(
                self.api_url,
                headers=headers,
                json=data,
                timeout=self.timeout,
                proxies=self._proxies(),
            )
            response.raise_for_status()
            result = response.json()
            
            # 根据讯飞 REST API 的响应格式提取内容
            if 'choices' in result and len(result['choices']) > 0:
                return result['choices'][0]['message']['content']
            else:
                return "抱歉，AI 服务暂时无法提供回答。"
                
        except requests.exceptions.RequestException as e:
            print(f"API 请求错误: {e}")
            return f"AI 服务请求失败: {str(e)}"
        except json.JSONDecodeError as e:
            print(f"JSON 解析错误: {e}")
            return "AI 服务返回了无效的响应格式。"
        except Exception as e:
            print(f"未知错误: {e}")
            return f"AI 服务出现错误: {str(e)}"


# 全局客户端实例
try:
    spark_client = XunfeiSparkClient()
except ValueError as e:
    print(f"警告: {e}")
    spark_client = None