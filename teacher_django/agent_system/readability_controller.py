"""可读性控制器（基于COGENT框架）

实现根据目标受众动态调整内容难度的功能，包括：
- 词汇难度控制
- 句子长度控制
- 概念抽象程度控制
- 可读性指标计算
"""
import re
from typing import Dict, List, Optional

# 词汇等级定义
VOCABULARY_LEVELS = {
    'basic': {
        'label': '基础词汇',
        'max_word_length': 6,
        'complexity': 1,
        'examples': ['学习', '知识', '简单', '重要', '理解'],
    },
    'intermediate': {
        'label': '中等词汇',
        'max_word_length': 8,
        'complexity': 2,
        'examples': ['概念', '原理', '分析', '应用', '理解'],
    },
    'advanced': {
        'label': '高级词汇',
        'max_word_length': 10,
        'complexity': 3,
        'examples': ['算法', '模型', '理论', '框架', '机制'],
    },
    'professional': {
        'label': '专业词汇',
        'max_word_length': 15,
        'complexity': 4,
        'examples': ['神经网络', '梯度下降', '卷积运算', '机器学习'],
    },
}

# 年级对应的词汇等级
GRADE_VOCAB_MAP = {
    'primary': 'basic',
    'junior': 'basic',
    'senior': 'intermediate',
    'college': 'advanced',
    'graduate': 'professional',
}


class ReadabilityController:
    """可读性控制器"""
    
    def __init__(self, target_level: str = 'college'):
        self.target_level = target_level
        self.vocab_level = GRADE_VOCAB_MAP.get(target_level, 'advanced')
        self.constraints = self._get_constraints(target_level)
    
    def _get_constraints(self, level: str) -> Dict:
        """获取可读性约束"""
        constraints = {
            'max_sentence_length': 35,
            'max_paragraph_length': 10,
            'vocab_complexity': 3,
            'max_word_length': 12,
            'require_examples': True,
            'examples_per_section': 2,
            'use_simple_grammar': False,
        }
        
        if level == 'primary':
            constraints.update({
                'max_sentence_length': 15,
                'max_paragraph_length': 3,
                'vocab_complexity': 1,
                'max_word_length': 6,
                'examples_per_section': 3,
                'use_simple_grammar': True,
            })
        elif level == 'junior':
            constraints.update({
                'max_sentence_length': 20,
                'max_paragraph_length': 5,
                'vocab_complexity': 1,
                'max_word_length': 7,
                'examples_per_section': 3,
                'use_simple_grammar': True,
            })
        elif level == 'senior':
            constraints.update({
                'max_sentence_length': 25,
                'max_paragraph_length': 7,
                'vocab_complexity': 2,
                'max_word_length': 9,
                'examples_per_section': 2,
                'use_simple_grammar': False,
            })
        elif level == 'college':
            constraints.update({
                'max_sentence_length': 35,
                'max_paragraph_length': 10,
                'vocab_complexity': 3,
                'max_word_length': 12,
                'examples_per_section': 2,
                'use_simple_grammar': False,
            })
        elif level == 'graduate':
            constraints.update({
                'max_sentence_length': 45,
                'max_paragraph_length': 15,
                'vocab_complexity': 4,
                'max_word_length': 15,
                'examples_per_section': 1,
                'use_simple_grammar': False,
            })
        
        return constraints
    
    def assess_readability(self, text: str) -> Dict:
        """评估文本的可读性"""
        sentences = self._split_sentences(text)
        words = self._split_words(text)
        
        # 计算指标
        avg_sentence_length = sum(len(s.split()) for s in sentences) / max(len(sentences), 1)
        avg_word_length = sum(len(w) for w in words) / max(len(words), 1)
        paragraph_count = self._count_paragraphs(text)
        
        # Flesch阅读难度指数（简化版）
        flesch_score = 206.835 - 1.015 * avg_sentence_length - 84.6 * (avg_word_length / max(avg_sentence_length, 1))
        
        # 词汇复杂度评估
        complex_word_ratio = sum(1 for w in words if len(w) > self.constraints['max_word_length']) / max(len(words), 1)
        
        return {
            'flesch_score': round(flesch_score, 2),
            'avg_sentence_length': round(avg_sentence_length, 2),
            'avg_word_length': round(avg_word_length, 2),
            'sentence_count': len(sentences),
            'word_count': len(words),
            'paragraph_count': paragraph_count,
            'complex_word_ratio': round(complex_word_ratio * 100, 2),
            'meets_constraints': self._check_constraints(avg_sentence_length, avg_word_length, paragraph_count, complex_word_ratio),
        }
    
    def _split_sentences(self, text: str) -> List[str]:
        """分割句子"""
        sentences = re.split(r'[。！？\.\?!]+', text)
        return [s.strip() for s in sentences if s.strip()]
    
    def _split_words(self, text: str) -> List[str]:
        """分割词语（中英文混合）"""
        # 匹配中文词语和英文单词
        pattern = r'[\u4e00-\u9fa5]+|[a-zA-Z]+'
        matches = re.findall(pattern, text)
        return [w for w in matches if w]
    
    def _count_paragraphs(self, text: str) -> int:
        """计算段落数"""
        paragraphs = re.split(r'\n\s*\n', text)
        return len([p for p in paragraphs if p.strip()])
    
    def _check_constraints(self, avg_sentence_length: float, avg_word_length: float, 
                          paragraph_count: int, complex_word_ratio: float) -> bool:
        """检查是否满足约束"""
        if avg_sentence_length > self.constraints['max_sentence_length']:
            return False
        if avg_word_length > self.constraints['max_word_length']:
            return False
        if complex_word_ratio > 0.3:  # 复杂词比例不超过30%
            return False
        return True
    
    def adjust_readability(self, text: str, topic: str = '') -> str:
        """调整文本可读性以符合目标水平"""
        constraints = self.constraints
        
        prompt = f"""
你是一位教育内容编辑专家，请根据以下要求调整文本的可读性。

【目标受众】{self.target_level}
【主题】{topic}

【可读性约束】
- 最大句子长度：{constraints['max_sentence_length']}个词
- 最大段落长度：{constraints['max_paragraph_length']}句
- 词汇难度：{VOCABULARY_LEVELS[self.vocab_level]['label']}
- 最大单词长度：{constraints['max_word_length']}个字符
- 是否使用简单语法：{constraints['use_simple_grammar']}
- 每节示例数量：{constraints['examples_per_section']}

【待调整文本】
{text[:3000]}

【调整要求】
1. 拆分过长的句子
2. 简化复杂词汇，用更简单的词替换
3. 保持原意不变
4. 添加适当的示例说明
5. 直接输出调整后的文本，不要额外说明

【输出】
调整后的完整文本
"""
        
        from .services.xinghuo_client import XinghuoClient
        client = XinghuoClient()
        adjusted_text = client.generate_text(prompt, max_tokens=3072)
        
        return adjusted_text
    
    def build_readability_prompt(self) -> str:
        """构建可读性约束提示词"""
        constraints = self.constraints
        vocab_label = VOCABULARY_LEVELS[self.vocab_level]['label']
        
        return f"""
【可读性要求】
- 目标受众：{self.target_level}
- 词汇难度：{vocab_label}（避免过于专业或复杂的词汇）
- 句子长度：每句不超过{constraints['max_sentence_length']}个词
- 段落长度：每段不超过{constraints['max_paragraph_length']}句
- 语法复杂度：{'使用简单语法结构' if constraints['use_simple_grammar'] else '可使用复杂语法'}
- 示例要求：每节至少{constraints['examples_per_section']}个具体示例
"""
    
    def suggest_simplification(self, text: str) -> Dict:
        """提供简化建议"""
        analysis = self.assess_readability(text)
        suggestions = []
        
        if analysis['avg_sentence_length'] > self.constraints['max_sentence_length']:
            suggestions.append(f"句子过长（平均{analysis['avg_sentence_length']}词），建议拆分成更短的句子")
        
        if analysis['avg_word_length'] > self.constraints['max_word_length']:
            suggestions.append(f"单词过长（平均{analysis['avg_word_length']}字符），建议使用更简单的词汇")
        
        if analysis['complex_word_ratio'] > 30:
            suggestions.append(f"复杂词比例过高（{analysis['complex_word_ratio']}%），建议简化专业术语")
        
        if analysis['flesch_score'] < 50:
            suggestions.append(f"可读性较低（Flesch指数{analysis['flesch_score']}），建议整体简化")
        
        return {
            'analysis': analysis,
            'suggestions': suggestions,
            'needs_adjustment': len(suggestions) > 0,
        }


class VocabularyManager:
    """词汇管理器"""
    
    # 常见专业术语及其简化版本
    TERM_SIMPLIFICATIONS = {
        '梯度下降': '逐步优化',
        '神经网络': '神经网络模型',
        '深度学习': '深度神经网络',
        '监督学习': '有老师指导的学习',
        '无监督学习': '自学式学习',
        '特征提取': '提取关键信息',
        '模型训练': '训练模型',
        '过拟合': '过度学习',
        '正则化': '防止过度学习',
        '卷积运算': '滑动计算',
        '递归神经网络': '循环神经网络',
        '自然语言处理': '语言处理',
        '计算机视觉': '图像识别',
        '数据挖掘': '数据探索',
        '机器学习': '让电脑自学',
        '人工智能': '智能技术',
        '算法复杂度': '算法效率',
        '时间复杂度': '运行时间',
        '空间复杂度': '内存使用',
        '动态规划': '分步求解',
        '贪心算法': '贪心策略',
        '回溯算法': '尝试所有可能',
        '二分查找': '折半查找',
        '快速排序': '快速排列',
        '堆排序': '堆式排列',
        '图论': '图形分析',
        '树结构': '树形结构',
        '链表': '链式列表',
        '栈': '堆叠结构',
        '队列': '排队结构',
        '哈希表': '快速查找表',
        '数据库': '数据仓库',
        'SQL': '数据库语言',
        '索引': '快速查找标记',
        '事务': '操作单元',
        '并发': '同时处理',
        '分布式': '分散处理',
        '云计算': '网络计算',
        'API': '程序接口',
        'REST': '接口标准',
        'JSON': '数据格式',
        'XML': '标记语言',
        'HTML': '网页语言',
        'CSS': '样式语言',
        'JavaScript': '网页脚本',
        'Python': '编程语言',
        '面向对象': '对象编程',
        '函数式编程': '函数编程',
        '模块化': '分块设计',
        '封装': '隐藏细节',
        '继承': '特性传递',
        '多态': '多种形态',
    }
    
    def simplify_terms(self, text: str, level: str = 'college') -> str:
        """简化专业术语"""
        if level in ['primary', 'junior']:
            # 小学和初中需要更多简化
            simplified_text = text
            for term, simplified in self.TERM_SIMPLIFICATIONS.items():
                # 对于低年级，用更简单的解释
                if level == 'primary':
                    simplified_text = simplified_text.replace(term, self._get_kid_friendly(term))
                else:
                    simplified_text = simplified_text.replace(term, simplified)
            return simplified_text
        return text  # 高中及以上保持原术语
    
    def _get_kid_friendly(self, term: str) -> str:
        """获取适合小学生的解释"""
        kid_friendly = {
            '梯度下降': '一步步找到最好的答案',
            '神经网络': '像大脑一样思考的电脑',
            '机器学习': '让电脑自己学本领',
            '人工智能': '会思考的机器人',
            '算法': '解决问题的步骤',
            '编程': '给电脑下指令',
            '数据': '有用的信息',
            '模型': '电脑学来的本领',
        }
        return kid_friendly.get(term, term)
    
    def add_glossary(self, text: str, terms: Optional[List[str]] = None) -> str:
        """添加术语表"""
        if not terms:
            # 自动提取专业术语
            terms = self._extract_terms(text)
        
        if not terms:
            return text
        
        glossary = "\n\n## 术语解释\n"
        for term in terms[:10]:
            simplified = self.TERM_SIMPLIFICATIONS.get(term, term)
            glossary += f"- **{term}**：{simplified}\n"
        
        return text + glossary
    
    def _extract_terms(self, text: str) -> List[str]:
        """提取文本中的专业术语"""
        terms = []
        for term in self.TERM_SIMPLIFICATIONS:
            if term in text:
                terms.append(term)
        return terms
