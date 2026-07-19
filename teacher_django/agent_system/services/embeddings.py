"""轻量文本向量化服务。

实现：词/双字 n-gram 的**哈希装桶（hashing trick）词袋向量** + L2 归一化。
相比早期"对整段文本取 sha256"的做法（不同文本得到的相似度是噪声），本实现下
"共享词越多的两段文本向量越接近"，cosine 相似度真正反映内容重合度，可作为
检索的语义信号（无需外部模型/依赖）。如需更强语义，可在此替换为讯飞星火 embedding。
"""
import hashlib
import math
import re
from typing import List


def _sanitize_embedding_text(text: str) -> str:
    if not text:
        return ''
    return ''.join(ch for ch in str(text) if not 0xD800 <= ord(ch) <= 0xDFFF)


def _tokens(text: str) -> List[str]:
    """英文/数字按词、中文按单字切分，并补相邻双字（近似词），用于词袋。"""
    base = re.findall(r'[a-zA-Z0-9]+|[一-鿿]', str(text).lower())
    grams = list(base)
    for i in range(len(base) - 1):
        grams.append(base[i] + base[i + 1])  # 相邻双字/双词，捕捉"词"级重合
    return grams


def compute_embedding(text: str, dim: int = 64) -> List[float]:
    """把文本映射为长度 dim 的 L2 归一化词袋向量（哈希装桶）。
    共享的词/双字越多，两段文本的向量越接近——cosine 相似度因此能真正反映内容重合。"""
    safe_text = _sanitize_embedding_text(text)
    if not safe_text:
        return [0.0] * dim
    vec = [0.0] * dim
    for g in _tokens(safe_text):
        idx = int(hashlib.md5(g.encode('utf-8')).hexdigest(), 16) % dim
        vec[idx] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
