"""简单的讯飞星火（Xinghuo）API 客户端封装。

说明：
- 该封装为通用 HTTP 封装，使用项目设置中的 `XINGHUO_API_URL` 和 `XINGHUO_API_KEY`。
- 不对外暴露真实请求格式细节，按讯飞实际 API 文档调整 `generate_text` 的 payload。
"""
import logging
import json
import random
import time
import requests
from typing import Optional
from django.conf import settings

logger = logging.getLogger(__name__)


class XinghuoClient:
    def __init__(self, api_url=None, api_key=None, default_model=None, timeout=None):
        self.api_url = api_url or settings.XINGHUO_API_URL
        self.api_key = api_key or settings.XINGHUO_API_KEY
        self.default_model = default_model or settings.XINGHUO_DEFAULT_MODEL
        self.timeout = timeout or getattr(settings, 'XINGHUO_TIMEOUT', 30)
        if not self.api_url or not self.api_key:
            logger.warning('XINGHUO_API_URL or XINGHUO_API_KEY not configured')

    def _placeholder_text(self, prompt: str) -> str:
        # 不再编造任何占位课程内容（假幻灯片/假题目/假讲义）。生成失败时统一返回一个
        # 明确的失败标记（带 `[占位` 前缀，供上层 _is_llm_failure 识别），由上层据此给用户
        # “生成失败，请重试”的反馈，而不是把以假乱真的内容写进课件。
        return '[占位] AI内容生成失败：接口暂时不可用，请稍后重试。'

    def _build_headers(self):
        api_key = (self.api_key or '').strip()
        return {
            'Content-Type': 'application/json',
            'Authorization': api_key,
        }

    def _proxies(self):
        # 只有外部讯飞星火云端点才需要走系统代理；本地或自建 Ollama（含远程 GPU 机器，如
        # 123.127.204.31）一律绕过系统代理，否则会被代理拦成 502/超时。
        u = (self.api_url or '').lower()
        if 'xf-yun.com' in u or 'xfyun.cn' in u:
            return None  # 星火：走系统代理
        return {'http': None, 'https': None}  # Ollama（本地/远程 GPU）：绕过代理

    def _extract_text(self, data):
        if isinstance(data, dict):
            if 'text' in data:
                return data['text']
            if 'result' in data and isinstance(data['result'], dict) and 'content' in data['result']:
                return data['result']['content']
            if 'choices' in data and isinstance(data['choices'], list) and len(data['choices']) > 0:
                ch = data['choices'][0]
                if isinstance(ch, dict):
                    if 'message' in ch and isinstance(ch['message'], dict) and 'content' in ch['message']:
                        return ch['message']['content']
                    if 'text' in ch:
                        return ch['text']
                    if 'content' in ch:
                        return ch['content']
        return str(data)

    def generate_text(self, prompt: str, model: Optional[str] = None, max_tokens: int = 1024) -> str:
        """对话/文本生成的简单同步请求封装。

        注意：请根据讯飞星火的真实接口文档调整 `payload` 的字段（如引擎、参数名等）。
        返回：解析后的文本结果（尽量提取常见字段），否则返回原始 JSON 字符串。
        """
        if not self.api_url or not self.api_key:
            logger.warning('Xinghuo API 未配置，返回占位文本')
            return self._placeholder_text(prompt)

        payload = {
            'model': model or self.default_model or 'lite',
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': max_tokens,
            'temperature': 0.5,
        }
        headers = self._build_headers()

        # 免费档（Spark Lite）并发/限流很低：多路并行或密集调用时服务端会用 5xx / 超时
        # 甩负载。单次请求几乎总能成功，所以这里对 5xx/429/超时做指数退避重试，
        # 只有多次仍失败才回退占位——避免网络抖动就把占位文本写进课件。
        max_retries = int(getattr(settings, 'XINGHUO_MAX_RETRIES', 3))
        last_reason = None
        for attempt in range(max_retries):
            try:
                resp = requests.post(self.api_url, json=payload, headers=headers, timeout=self.timeout, proxies=self._proxies())
                if resp.status_code >= 500 or resp.status_code == 429:
                    last_reason = f'HTTP {resp.status_code}'
                    if attempt < max_retries - 1:
                        backoff = min(2 ** attempt, 6) + random.uniform(0, 0.6)
                        logger.warning('Xinghuo %s（第%d/%d次），退避 %.1fs 后重试',
                                       resp.status_code, attempt + 1, max_retries, backoff)
                        time.sleep(backoff)
                        continue
                    logger.warning('Xinghuo 多次 %s，回退占位', resp.status_code)
                    break
                resp.raise_for_status()
                try:
                    data = resp.json()
                except Exception:
                    logger.exception('Xinghuo 返回非 JSON')
                    return resp.text
                return self._extract_text(data)
            except requests.exceptions.Timeout as exc:
                last_reason = exc
                if attempt < max_retries - 1:
                    backoff = min(2 ** attempt, 6) + random.uniform(0, 0.6)
                    logger.warning('Xinghuo 超时（第%d/%d次），退避 %.1fs 后重试', attempt + 1, max_retries, backoff)
                    time.sleep(backoff)
                    continue
                break
            except requests.exceptions.RequestException as exc:
                # 连接错误等非超时异常：重试一次即可，避免长时间卡住
                last_reason = exc
                if attempt < 1:
                    time.sleep(0.5 + random.uniform(0, 0.5))
                    continue
                break

        logger.warning('Xinghuo request failed, falling back to placeholder: %s', last_reason)
        return self._placeholder_text(prompt)

    @staticmethod
    def _parse_stream_line(line: str) -> Optional[str]:
        """把 OpenAI 兼容 SSE 的一行解析成**纯文本增量**。
        返回 None 表示这一行没有内容（心跳/[DONE]/空行/解析失败），调用方跳过。
        以前 stream_generate 直接把 `data: {json}` 原样吐出去，前端拿到的是 JSON 而非文本，
        所以流式一直没被前端采用——这里负责抽出 choices[0].delta.content。"""
        if not line:
            return None
        s = line.strip()
        if s.startswith('data:'):
            s = s[5:].strip()
        if not s or s == '[DONE]':
            return None
        try:
            obj = json.loads(s)
        except Exception:
            return None
        try:
            choices = obj.get('choices') or []
            if choices:
                ch = choices[0] or {}
                delta = ch.get('delta') or {}
                content = delta.get('content')
                if content is None:
                    content = (ch.get('message') or {}).get('content') or ch.get('text')
                return content or None
        except Exception:
            return None
        return None

    def stream_generate(self, prompt: str, model: Optional[str] = None, max_tokens: int = 1024):
        """流式生成：逐段产出**纯文本增量**（已解析 OpenAI 兼容 SSE）。
        不再输出任何 `[占位流]` 假内容——接口未配置/失败时直接结束（产出为空），
        由调用方/前端回退到非流式接口，避免把演示占位当成真答案。"""
        if not self.api_url or not self.api_key:
            logger.warning('Xinghuo API 未配置，流式直接结束（前端回退非流式）')
            return

        payload = {
            'model': model or self.default_model or 'lite',
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': max_tokens,
            'temperature': 0.5,
            'stream': True,
        }
        headers = self._build_headers()
        try:
            with requests.post(self.api_url, json=payload, headers=headers, timeout=self.timeout, stream=True, proxies=self._proxies()) as resp:
                if resp.status_code >= 400:
                    logger.warning('Xinghuo stream HTTP %s，流式结束', resp.status_code)
                    return
                # 强制按 UTF-8 解码：本地 Ollama 的 text/event-stream 不带 charset，
                # requests 默认按 ISO-8859-1 解，中文会变成 [object Object] 那种乱码
                resp.encoding = 'utf-8'
                for line in resp.iter_lines(decode_unicode=True):
                    piece = self._parse_stream_line(line)
                    if piece:
                        yield piece
        except requests.exceptions.RequestException as exc:
            logger.warning('Xinghuo stream request failed: %s', exc)
            return
