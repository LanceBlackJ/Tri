"""内容安全与防幻觉工具

提供完整的内容安全过滤和事实一致性检查机制：
1. 违禁词过滤（基于词表和讯飞API）
2. 敏感内容检测（政治、色情、暴力等）
3. 事实一致性检查（防幻觉）
4. 学术内容验证
"""
from django.conf import settings
import logging
import json
import re

logger = logging.getLogger(__name__)
try:
    from .xinghuo_client import XinghuoClient
except Exception:
    XinghuoClient = None

# 违禁词分类
BANLIST_CATEGORIES = {
    'politics': [
        '敏感政治人物', '敏感政治事件', '分裂主义', '极端主义', '恐怖主义',
        '颠覆', '煽动', '暴乱', '非法组织', '邪教'
    ],
    'pornography': [
        '色情', '色情内容', '色情图片', '色情视频', '性交易', '卖淫', '嫖娼',
        '裸露', '挑逗', '低俗'
    ],
    'violence': [
        '暴力', '血腥', '屠杀', '谋杀', '自杀', '自残', '攻击', '伤害',
        '武器', '枪械', '爆炸'
    ],
    'drugs': [
        '毒品', '鸦片', '海洛因', '冰毒', '大麻', '可卡因', '兴奋剂',
        '走私', '贩毒'
    ],
    'fraud': [
        '诈骗', '传销', '非法集资', '虚假宣传', '假冒', '伪造', '欺骗'
    ],
    'others': [
        '辱骂', '诽谤', '谣言', '歧视', '仇恨', '人肉搜索', '隐私'
    ]
}

# 合并所有违禁词
BANLIST = []
for category in BANLIST_CATEGORIES.values():
    BANLIST.extend(category)

# 教育/历史/学术语境下合法、但恰好含有违禁子串的短语——不应被判违禁或打码。
# （此前"鸦片战争"里的"鸦片"被当毒品打码成"**战争"、"南京大屠杀"被打成"南京**"等。）
# 匹配时先保护这些短语，再扫描/打码其余部分。可按需扩充。
PROTECTED_PHRASES = [
    '第一次鸦片战争', '第二次鸦片战争', '鸦片战争', '虎门销烟', '禁烟运动', '林则徐禁烟',
    '南京大屠杀', '大屠杀纪念', '大屠杀',
    '暴力革命', '非暴力不合作', '非暴力',
    '化学武器', '生化武器', '核武器', '常规武器', '武器装备',
    '禁毒', '缉毒', '戒毒', '毒品预防教育', '毒品危害',
    '反诈骗', '防诈骗', '识别诈骗', '电信诈骗防范',
]


def _protect_phrases(text):
    """把受保护短语替换成占位符，返回 (处理后文本, 占位符->原短语 映射)。"""
    placeholders = {}
    out = text
    # 长短语优先，避免子短语先命中
    for i, phrase in enumerate(sorted(PROTECTED_PHRASES, key=lambda x: -len(x))):
        if phrase and phrase in out:
            ph = f'\x00P{i}\x00'
            placeholders[ph] = phrase
            out = out.replace(phrase, ph)
    return out, placeholders

# 学术内容检查关键词（用于检测事实性错误）
ACADEMIC_CHECK_KEYWORDS = [
    '定理', '定律', '公式', '证明', '结论', '研究表明', '根据', '引用',
    '发表于', '作者', '论文', '期刊', '会议', '实验', '数据表明'
]


def check_text(text: str) -> dict:
    """检查文本是否包含违禁词，返回详细结果。"""
    if not text:
        return {'safe': True, 'found': [], 'categories': [], 'score': 0}

    # 先保护合法的教育/历史短语，避免其中的违禁子串误报
    scan_text, _ = _protect_phrases(text)

    found = []
    categories = []

    for category, words in BANLIST_CATEGORIES.items():
        for word in words:
            if word in scan_text:
                found.append(word)
                if category not in categories:
                    categories.append(category)
    
    score = min(len(found) * 20, 100)
    
    return {
        'safe': len(found) == 0,
        'found': found,
        'categories': categories,
        'score': score
    }


def censor_text(text: str) -> str:
    """用星号替换违禁词，返回处理后的文本。"""
    if not text:
        return text

    # 先保护合法的教育/历史短语（如"鸦片战争""南京大屠杀"），打码后再还原
    out, placeholders = _protect_phrases(text)

    # 按词长度降序排列，避免短词先替换导致长词匹配失败
    sorted_words = sorted(BANLIST, key=lambda x: -len(x))

    for word in sorted_words:
        word = word.strip()
        if not word:
            continue
        out = out.replace(word, '*' * len(word))

    # 还原受保护短语
    for ph, phrase in placeholders.items():
        out = out.replace(ph, phrase)

    return out


def check_with_xinghuo(text: str) -> dict:
    """使用讯飞星火进行安全分类。

    返回 {'safe': bool, 'labels': [...], 'categories': [], 'score': int, 'notes': '...'}。
    """
    if not XinghuoClient:
        result = check_text(text)
        result['notes'] = 'xinghuo client not available, using fallback'
        return result
    
    if not getattr(settings, 'XINGHUO_API_URL', None) or not getattr(settings, 'XINGHUO_API_KEY', None):
        result = check_text(text)
        result['notes'] = 'xinghuo not configured, using fallback'
        return result

    # 优先使用 core.xunfei_spark 的客户端
    try:
        from core.xunfei_spark import spark_client, XunfeiSparkClient
        if spark_client:
            client = spark_client
        else:
            try:
                client = XunfeiSparkClient()
            except Exception:
                client = XinghuoClient()
    except Exception:
        client = XinghuoClient()

    prompt = f"""请对以下文本进行安全评估，并返回严格的JSON格式：
文本：{text}

请按照以下格式返回JSON：
{{
    "safe": true/false,
    "labels": ["标签1", "标签2"],
    "categories": ["category1", "category2"],
    "score": 0-100,
    "notes": "评估说明"
}}

分类说明：
- politics: 政治敏感
- pornography: 色情低俗
- violence: 暴力血腥
- drugs: 毒品相关
- fraud: 诈骗虚假
- others: 其他违规

评分标准：
- 0-20: 安全
- 21-50: 轻微风险
- 51-80: 中等风险
- 81-100: 高风险
"""
    
    try:
        if hasattr(client, 'get_response') and not hasattr(client, 'generate_text'):
            msgs = [{"role": "user", "content": prompt}]
            resp = client.get_response(msgs)
        else:
            resp = client.generate_text(prompt)
        
        try:
            parsed = json.loads(resp)
            if isinstance(parsed, dict) and 'safe' in parsed:
                # 确保返回格式正确
                result = {
                    'safe': parsed.get('safe', True),
                    'labels': parsed.get('labels', []),
                    'categories': parsed.get('categories', []),
                    'score': parsed.get('score', 0),
                    'notes': parsed.get('notes', 'Xinghuo API')
                }
                return result
        except json.JSONDecodeError:
            logger.debug('Xinghuo 安全检查未返回有效 JSON: %s', resp)
        
        # 回退到本地检查
        result = check_text(text)
        result['notes'] = 'API response invalid, using fallback'
        return result
        
    except Exception as e:
        logger.exception('调用讯飞合规检查失败')
        result = check_text(text)
        result['notes'] = f'error: {str(e)}'
        return result


def verify_factuality(text: str, topic: str = '') -> dict:
    """检查文本的事实一致性（防幻觉）。

    返回 {'reliable': bool, 'confidence': 0-100, 'suggestions': [...], 'warnings': [...]}
    """
    if not text:
        return {'reliable': True, 'confidence': 100, 'suggestions': [], 'warnings': []}
    
    warnings = []
    suggestions = []
    confidence = 80  # 默认置信度
    
    # 1. 检查是否有明显的事实错误模式
    # 检查日期/数字是否合理
    year_pattern = r'(19[0-9]{2}|20[0-9]{2})'
    years = re.findall(year_pattern, text)
    for year in years:
        year_int = int(year)
        if year_int < 1900 or year_int > 2100:
            warnings.append(f"发现异常年份: {year}")
            confidence -= 10
    
    # 2. 检查学术内容是否有可疑模式
    has_academic_claim = any(keyword in text for keyword in ACADEMIC_CHECK_KEYWORDS)
    
    if has_academic_claim:
        # 检查是否有引用格式
        if not re.search(r'(\[\d+\]|\(\d+\)|引用|参考文献)', text):
            suggestions.append("建议添加引用来源以增强可信度")
            confidence -= 5
        
        # 检查是否有过于绝对的表述（去掉"全部/所有/完全"——它们在学术正文里常是正常用词，会误伤）
        absolute_words = ['绝对', '一定', '必须', '唯一']
        for word in absolute_words:
            if word in text:
                suggestions.append(f"注意使用 '{word}' 等绝对化表述")
                confidence -= 3

    # 3. 检查逻辑一致性（简单检查）
    # 注意：去掉了 ('全部','部分') —— "全部内容分为几个部分"这类正常表述会共现，属稳定误报
    contradictory_patterns = [
        ('不可能', '一定'),
        ('从未', '经常'),
    ]
    for p1, p2 in contradictory_patterns:
        if p1 in text and p2 in text:
            warnings.append(f"检测到潜在矛盾表述: '{p1}' 和 '{p2}'")
            confidence -= 15
    
    # 4. 使用大模型进行深度事实检查
    if topic and XinghuoClient:
        try:
            client = XinghuoClient()
            fact_check_prompt = f"""请检查以下关于"{topic}"的内容是否存在事实错误或幻觉：

内容：
{text[:1000]}

请分析：
1. 是否存在明显的事实错误？
2. 是否存在未经证实的断言？
3. 是否存在逻辑矛盾？

请返回JSON格式：
{{
    "has_errors": true/false,
    "errors": ["错误1", "错误2"],
    "suggestions": ["建议1", "建议2"]
}}
"""
            if hasattr(client, 'get_response'):
                msgs = [{"role": "user", "content": fact_check_prompt}]
                resp = client.get_response(msgs)
            else:
                resp = client.generate_text(fact_check_prompt)
            
            try:
                parsed = json.loads(resp)
                if parsed.get('has_errors', False):
                    warnings.extend(parsed.get('errors', []))
                    suggestions.extend(parsed.get('suggestions', []))
                    confidence = max(confidence - 20, 0)
            except json.JSONDecodeError:
                pass
        except Exception as e:
            logger.debug('事实检查调用失败: %s', e)
    
    # 确保置信度在合理范围
    confidence = max(0, min(100, confidence))
    
    return {
        'reliable': confidence >= 70,
        'confidence': confidence,
        'suggestions': suggestions,
        'warnings': warnings
    }


def verify_academic_content(text: str, topic: str = '') -> dict:
    """验证学术内容的准确性和规范性。"""
    result = {
        'valid': True,
        'issues': [],
        'suggestions': [],
        'score': 100
    }
    
    if not text:
        return result
    
    # 检查格式规范
    # 1. 检查是否有明显的格式问题
    if len(text) < 50:
        result['issues'].append("内容过短，可能不够完整")
        result['score'] -= 10
    
    # 2. 检查学术规范
    if '参考文献' not in text and '引用' not in text and '[1]' not in text:
        result['suggestions'].append("建议添加参考文献或引用来源")
        result['score'] -= 5
    
    # 3. 检查公式和术语（简单检查）
    has_formula = re.search(r'([a-zA-Z]+\s*=\s*.+|\\[a-zA-Z]+|Σ|∫|∑)', text)
    if has_formula:
        # 检查公式是否完整
        if '=' not in text:
            result['suggestions'].append("检查公式是否完整")
            result['score'] -= 5
    
    # 4. 检查是否有代码块
    has_code = re.search(r'```[\s\S]*```', text)
    if has_code:
        # 检查代码是否有语法问题（简单检查）
        code_block = re.search(r'```(\w+)?\n([\s\S]*?)```', text)
        if code_block:
            language = code_block.group(1)
            code_content = code_block.group(2)
            # 简单检查：Python代码是否有基本结构
            if language == 'python' and ('def ' not in code_content and 'import ' not in code_content):
                result['suggestions'].append("建议添加完整的Python代码结构")
    
    # 5. 调用事实检查
    fact_result = verify_factuality(text, topic)
    if not fact_result['reliable']:
        result['issues'].extend(fact_result['warnings'])
        result['suggestions'].extend(fact_result['suggestions'])
        result['score'] = min(result['score'], fact_result['confidence'])
    
    result['valid'] = result['score'] >= 70
    
    return result


def comprehensive_check(text: str, topic: str = '', content_type: str = 'general') -> dict:
    """综合安全检查，包含所有检查项。"""
    results = {
        'overall': {
            'safe': True,
            'reliable': True,
            'score': 100
        },
        'safety': None,
        'factuality': None,
        'academic': None
    }
    
    # 1. 安全检查
    safety_result = check_with_xinghuo(text)
    results['safety'] = safety_result
    
    if not safety_result['safe']:
        results['overall']['safe'] = False
        results['overall']['score'] -= (100 - safety_result['score']) // 2
    
    # 2. 事实性检查（学术内容必查）
    if content_type in ['academic', 'doc', 'course', 'research']:
        factuality_result = verify_factuality(text, topic)
        results['factuality'] = factuality_result
        
        if not factuality_result['reliable']:
            results['overall']['reliable'] = False
            results['overall']['score'] = min(results['overall']['score'], factuality_result['confidence'])
    
    # 3. 学术内容验证
    if content_type in ['academic', 'doc', 'course']:
        academic_result = verify_academic_content(text, topic)
        results['academic'] = academic_result
        results['overall']['score'] = min(results['overall']['score'], academic_result['score'])
    
    # 综合评分
    scores = [results['overall']['score']]
    if results['safety']:
        scores.append(results['safety']['score'])
    if results['factuality']:
        scores.append(results['factuality']['confidence'])
    if results['academic']:
        scores.append(results['academic']['score'])
    
    results['overall']['score'] = int(sum(scores) / len(scores))
    results['overall']['safe'] = results['overall']['score'] >= 70
    
    return results


def sanitize_content(text: str, topic: str = '', content_type: str = 'general') -> dict:
    """清理和验证内容，返回处理后的结果。"""
    result = {
        'content': text,
        'sanitized': False,
        'rejected': False,
        'warnings': [],
        'suggestions': [],
        'score': 100
    }
    
    # 1. 先进行安全检查
    safety_result = check_with_xinghuo(text)
    
    if not safety_result['safe']:
        # 尝试脱敏处理
        sanitized = censor_text(text)
        if sanitized != text:
            result['content'] = sanitized
            result['sanitized'] = True
            result['warnings'].append("内容包含敏感词汇，已进行脱敏处理")
        else:
            # 无法脱敏，拒绝内容
            result['rejected'] = True
            result['score'] = 0
            result['warnings'].append("内容包含无法脱敏的敏感内容")
            return result
    
    # 2. 学术内容检查
    if content_type in ['academic', 'doc', 'course']:
        academic_result = verify_academic_content(text, topic)
        result['suggestions'].extend(academic_result['suggestions'])
        result['score'] = min(result['score'], academic_result['score'])
    
    # 3. 事实性检查
    factuality_result = verify_factuality(text, topic)
    result['warnings'].extend(factuality_result['warnings'])
    result['suggestions'].extend(factuality_result['suggestions'])
    result['score'] = min(result['score'], factuality_result['confidence'])
    
    return result
