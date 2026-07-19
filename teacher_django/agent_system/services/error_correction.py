"""
错题检测与纠正模块 - 基于论文 arXiv:2503.11733v2

论文核心实现：
- 错误检测 (Error Detection)
- 错误分类 (Error Classification)
- 错误原因分析 (Misconception Analysis)
- 智能纠正策略 (Intelligent Correction)
- 自适应错误反馈 (Adaptive Feedback)
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict, Counter

logger = logging.getLogger(__name__)


class ErrorType(Enum):
    """
    错误类型分类
    """
    # 知识错误
    KNOWLEDGE_GAP = "knowledge_gap"           # 知识空白
    MISCONCEPTION = "misconception"          # 错误概念
    FORGOTTEN_KNOWLEDGE = "forgotten"       # 遗忘知识
    
    # 计算错误
    CALCULATION_ERROR = "calculation"        # 计算错误
    ARITHMETIC_MISTAKE = "arithmetic"        # 算术错误
    FORMULA_MISUSE = "formula"               # 公式误用
    
    # 理解错误
    CONCEPTUAL_ERROR = "conceptual"          # 概念错误
    INTERPRETATION_ERROR = "interpretation"  # 理解错误
    CONTEXT_ERROR = "context"               # 上下文理解错误
    
    # 逻辑错误
    LOGICAL_ERROR = "logical"                # 逻辑错误
    REASONING_ERROR = "reasoning"           # 推理错误
    INFERENCE_ERROR = "inference"           # 推断错误
    
    # 表达错误
    TYPO = "typo"                           # 拼写错误
    GRAMMAR_ERROR = "grammar"               # 语法错误
    EXPRESSION_ERROR = "expression"         # 表达错误
    
    # 策略错误
    STRATEGY_ERROR = "strategy"            # 策略错误
    METHOD_ERROR = "method"                  # 方法错误
    APPROACH_ERROR = "approach"             # 方法选择错误
    
    # 其他
    CARELESS_ERROR = "careless"             # 粗心错误
    TIME_PRESSURE = "time_pressure"         # 时间压力
    UNKNOWN = "unknown"                     # 未知错误


class ErrorSeverity(Enum):
    """
    错误严重程度
    """
    MINOR = 1     # 轻微：不影响整体理解
    MODERATE = 2 # 中等：影响部分理解
    SEVERE = 3   # 严重：导致完全错误
    CRITICAL = 4 # 关键：概念性根本错误


@dataclass
class ErrorPattern:
    """
    错误模式
    
    存储常见的错误模式及其特征
    """
    pattern_id: str
    error_type: ErrorType
    
    # 模式特征
    indicators: List[str] = field(default_factory=list)  # 指示词
    regex_patterns: List[str] = field(default_factory=list)  # 正则表达式
    context_patterns: List[str] = field(default_factory=list)  # 上下文模式
    
    # 关联信息
    common_misconceptions: List[str] = field(default_factory=list)  # 常见错误概念
    related_concepts: List[str] = field(default_factory=list)  # 相关知识点
    
    # 纠正策略
    correction_hints: List[str] = field(default_factory=list)  # 纠正提示
    learning_resources: Dict[str, str] = field(default_factory=dict)  # 学习资源
    
    # 统计信息
    frequency: int = 0
    success_rate_of_correction: float = 0.0
    
    def to_dict(self) -> Dict:
        return {
            'pattern_id': self.pattern_id,
            'error_type': self.error_type.value,
            'indicators': self.indicators,
            'regex_patterns': self.regex_patterns,
            'context_patterns': self.context_patterns,
            'common_misconceptions': self.common_misconceptions,
            'related_concepts': self.related_concepts,
            'correction_hints': self.correction_hints,
            'frequency': self.frequency,
            'success_rate': self.success_rate_of_correction
        }


@dataclass
class DetectedError:
    """
    检测到的错误
    """
    error_id: str
    error_type: ErrorType
    severity: ErrorSeverity
    
    # 错误位置
    location: Dict = field(default_factory=dict)  # {'start': x, 'end': y, 'line': n}
    
    # 错误内容
    user_answer: str = ""
    expected_answer: str = ""
    error_description: str = ""
    
    # 分析结果
    root_cause: str = ""
    misconceptions: List[str] = field(default_factory=list)
    related_concepts: List[str] = field(default_factory=list)
    
    # 纠正信息
    correction: str = ""
    explanation: str = ""
    hints: List[str] = field(default_factory=list)
    
    # 元数据
    confidence: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict:
        return {
            'error_id': self.error_id,
            'error_type': self.error_type.value,
            'severity': self.severity.value,
            'location': self.location,
            'user_answer': self.user_answer,
            'expected_answer': self.expected_answer,
            'error_description': self.error_description,
            'root_cause': self.root_cause,
            'misconceptions': self.misconceptions,
            'related_concepts': self.related_concepts,
            'correction': self.correction,
            'explanation': self.explanation,
            'hints': self.hints,
            'confidence': self.confidence,
            'timestamp': self.timestamp.isoformat()
        }


@dataclass
class CorrectionFeedback:
    """
    纠正反馈
    
    包含面向用户的完整纠正信息
    """
    error: DetectedError
    
    # 反馈级别
    feedback_level: str = "detailed"  # minimal, moderate, detailed
    
    # 反馈内容
    empathetic_intro: str = ""  # 同理心引导
    error_explanation: str = ""
    correct_answer: str = ""
    step_by_step_solution: List[str] = field(default_factory=list)
    
    # 后续建议
    related_concepts_to_review: List[str] = field(default_factory=list)
    practice_recommendations: List[str] = field(default_factory=list)
    learning_path_suggestions: List[str] = field(default_factory=list)
    
    # 鼓励信息
    encouragement: str = ""
    
    def to_dict(self) -> Dict:
        return {
            'error': self.error.to_dict(),
            'feedback_level': self.feedback_level,
            'empathetic_intro': self.empathetic_intro,
            'error_explanation': self.error_explanation,
            'correct_answer': self.correct_answer,
            'step_by_step_solution': self.step_by_step_solution,
            'related_concepts_to_review': self.related_concepts_to_review,
            'practice_recommendations': self.practice_recommendations,
            'learning_path_suggestions': self.learning_path_suggestions,
            'encouragement': self.encouragement
        }
    
    def to_user_message(self) -> str:
        """
        转换为面向用户的消息
        """
        parts = []
        
        # 同理心引导
        if self.empathetic_intro:
            parts.append(self.empathetic_intro)
        
        # 错误解释
        if self.error_explanation:
            parts.append(self.error_explanation)
        
        # 正确答案
        if self.correct_answer:
            parts.append(f"正确的答案是：**{self.correct_answer}**")
        
        # 分步解答
        if self.step_by_step_solution:
            parts.append("让我一步步解释：")
            for i, step in enumerate(self.step_by_step_solution, 1):
                parts.append(f"{i}. {step}")
        
        # 后续建议
        if self.related_concepts_to_review:
            concepts = "、".join(self.related_concepts_to_review)
            parts.append(f"建议复习相关概念：{concepts}")
        
        # 鼓励
        if self.encouragement:
            parts.append(self.encouragement)
        
        return "\n\n".join(parts)


class ErrorPatternLibrary:
    """
    错误模式库
    
    存储和管理常见错误模式
    """
    
    def __init__(self):
        """初始化错误模式库"""
        self.patterns: Dict[str, ErrorPattern] = {}
        self._initialize_common_patterns()
    
    def _initialize_common_patterns(self):
        """初始化常见错误模式"""
        # 数学计算错误
        self.add_pattern(ErrorPattern(
            pattern_id="sign_error",
            error_type=ErrorType.CALCULATION_ERROR,
            indicators=["负号", "符号", "+-", "-+", "正负"],
            common_misconceptions=["符号移动时忘记变号", "括号前是负号时去括号错误"],
            correction_hints=[
                "检查每一步的符号变化",
                "去括号时，括号前是负号要变号"
            ],
            related_concepts=["有理数运算", "整式加减"]
        ))
        
        self.add_pattern(ErrorPattern(
            pattern_id="distribution_error",
            error_type=ErrorType.CALCULATION_ERROR,
            indicators=["分配", "分配律", "乘法分配律", "×"],
            common_misconceptions=["a(b+c)=ab+c", "漏乘"],
            correction_hints=[
                "乘法分配律: a(b+c)=ab+ac",
                "确保每一项都乘到"
            ],
            related_concepts=["乘法分配律", "整式乘法"]
        ))
        
        self.add_pattern(ErrorPattern(
            pattern_id="fraction_error",
            error_type=ErrorType.CALCULATION_ERROR,
            indicators=["分数", "分母", "分子", "/"],
            common_misconceptions=["分子分母同时加减错误", "通分错误"],
            correction_hints=[
                "通分时找最小公倍数",
                "分子加减时父母不变"
            ],
            related_concepts=["分式运算", "通分与约分"]
        ))
        
        # 概念理解错误
        self.add_pattern(ErrorPattern(
            pattern_id="definition_misconception",
            error_type=ErrorType.MISCONCEPTION,
            indicators=["定义", "概念", "什么是", "的意思"],
            common_misconceptions=["混淆相近概念", "遗漏关键条件"],
            correction_hints=[
                "仔细阅读定义的所有条件",
                "区分相似概念的关键差异"
            ],
            related_concepts=[]
        ))
        
        # 逻辑推理错误
        self.add_pattern(ErrorPattern(
            pattern_id="causation_confusion",
            error_type=ErrorType.LOGICAL_ERROR,
            indicators=["因为", "所以", "导致", "引起"],
            common_misconceptions=["因果关系颠倒", "相关当作因果"],
            correction_hints=[
                "因果关系的方向要正确",
                "注意区分相关性和因果性"
            ],
            related_concepts=["逻辑推理", "因果分析"]
        ))
    
    def add_pattern(self, pattern: ErrorPattern):
        """添加错误模式"""
        self.patterns[pattern.pattern_id] = pattern
    
    def detect_pattern(
        self,
        user_answer: str,
        context: Optional[Dict] = None
    ) -> List[Tuple[ErrorPattern, float]]:
        """
        检测匹配的错误模式
        
        Args:
            user_answer: 用户答案
            context: 上下文信息
            
        Returns:
            [(匹配的模式, 匹配度), ...]
        """
        matches = []
        context = context or {}
        
        for pattern in self.patterns.values():
            score = 0.0
            match_count = 0
            
            # 检查指示词
            for indicator in pattern.indicators:
                if indicator.lower() in user_answer.lower():
                    score += 0.3
                    match_count += 1
            
            # 检查正则表达式
            for regex in pattern.regex_patterns:
                if re.search(regex, user_answer, re.IGNORECASE):
                    score += 0.4
                    match_count += 1
            
            # 检查上下文模式
            if context:
                for ctx_pattern in pattern.context_patterns:
                    if ctx_pattern.lower() in str(context).lower():
                        score += 0.2
                        match_count += 1
            
            # 归一化分数
            if match_count > 0:
                normalized_score = min(score, 1.0)
                matches.append((pattern, normalized_score))
        
        # 按匹配度排序
        matches.sort(key=lambda x: x[1], reverse=True)
        return matches
    
    def get_pattern(self, pattern_id: str) -> Optional[ErrorPattern]:
        """获取指定模式"""
        return self.patterns.get(pattern_id)
    
    def get_patterns_by_type(
        self,
        error_type: ErrorType
    ) -> List[ErrorPattern]:
        """获取指定类型的模式"""
        return [
            p for p in self.patterns.values()
            if p.error_type == error_type
        ]


class ErrorAnalyzer:
    """
    错误分析器
    
    分析错误原因和类型
    """
    
    # 错误类型关键词
    ERROR_KEYWORDS = {
        ErrorType.CALCULATION_ERROR: [
            '计算', '算', '运算', '结果', '得', '等于',
            'calculate', 'compute', 'result'
        ],
        ErrorType.CONCEPTUAL_ERROR: [
            '概念', '定义', '理解', '什么', '意思', '为什么',
            'concept', 'definition', 'meaning', 'what'
        ],
        ErrorType.LOGICAL_ERROR: [
            '因为', '所以', '如果', '那么', '推理', '推断',
            'because', 'therefore', 'if', 'then', 'reasoning'
        ],
        ErrorType.MISCONCEPTION: [
            '认为', '觉得', '好像', '可能', '误解',
            'think', 'believe', 'maybe', 'misunderstand'
        ],
        ErrorType.KNOWLEDGE_GAP: [
            '不知道', '没学过', '没学过', '没教过',
            "don't know", "didn't learn", 'never seen'
        ]
    }
    
    def __init__(self, llm_client=None):
        """
        初始化错误分析器
        
        Args:
            llm_client: LLM客户端，用于深度分析
        """
        self.llm_client = llm_client
        self.pattern_library = ErrorPatternLibrary()
    
    def analyze_error(
        self,
        user_answer: str,
        expected_answer: str,
        question: str,
        context: Optional[Dict] = None
    ) -> DetectedError:
        """
        分析错误
        
        Args:
            user_answer: 用户答案
            expected_answer: 期望答案
            question: 问题
            context: 上下文
            
        Returns:
            DetectedError: 分析结果
        """
        context = context or {}
        
        # 1. 确定错误类型
        error_type = self._classify_error_type(
            user_answer, expected_answer, question
        )
        
        # 2. 确定错误严重程度
        severity = self._determine_severity(
            user_answer, expected_answer
        )
        
        # 3. 检测错误模式
        pattern_matches = self.pattern_library.detect_pattern(
            user_answer, context
        )
        
        # 4. 分析根本原因
        root_cause = self._analyze_root_cause(
            user_answer, expected_answer, error_type
        )
        
        # 5. 识别相关错误概念
        misconceptions = self._identify_misconceptions(
            user_answer, expected_answer, error_type, pattern_matches
        )
        
        # 6. 生成纠正建议
        correction = self._generate_correction(
            user_answer, expected_answer, error_type, pattern_matches
        )
        
        # 7. 生成解释
        explanation = self._generate_explanation(
            user_answer, expected_answer, error_type, root_cause
        )
        
        # 8. 生成提示
        hints = self._generate_hints(
            error_type, pattern_matches, misconceptions
        )
        
        # 计算置信度
        confidence = self._calculate_confidence(
            error_type, severity, pattern_matches
        )
        
        return DetectedError(
            error_id=self._generate_error_id(),
            error_type=error_type,
            severity=severity,
            location=context.get('location', {}),
            user_answer=user_answer,
            expected_answer=expected_answer,
            error_description=self._describe_error(error_type, severity),
            root_cause=root_cause,
            misconceptions=misconceptions,
            related_concepts=self._extract_related_concepts(pattern_matches),
            correction=correction,
            explanation=explanation,
            hints=hints,
            confidence=confidence
        )
    
    def _classify_error_type(
        self,
        user_answer: str,
        expected_answer: str,
        question: str
    ) -> ErrorType:
        """
        分类错误类型
        """
        # 简单检查答案差异类型
        user_lower = user_answer.lower()
        expected_lower = expected_answer.lower()
        
        # 检查是否是拼写/打字错误
        if len(user_answer) > 0 and len(expected_answer) > 0:
            diff_count = sum(
                1 for a, b in zip(user_lower, expected_lower) if a != b
            )
            if diff_count == 1 or diff_count == 2:
                if abs(len(user_answer) - len(expected_answer)) <= 2:
                    return ErrorType.TYPO
        
        # 检查关键词匹配
        for error_type, keywords in self.ERROR_KEYWORDS.items():
            match_count = sum(1 for kw in keywords if kw.lower() in question.lower())
            if match_count > 0:
                return error_type
        
        # 检查数值计算错误
        try:
            user_num = float(user_answer)
            expected_num = float(expected_answer)
            
            # 检查是否只是符号错误
            if abs(user_num - expected_num) < abs(user_num) * 0.1:
                return ErrorType.CALCULATION_ERROR
        except:
            pass
        
        # 默认分类
        return ErrorType.UNKNOWN
    
    def _determine_severity(
        self,
        user_answer: str,
        expected_answer: str
    ) -> ErrorSeverity:
        """
        确定错误严重程度
        """
        # 完全正确
        if user_answer.strip() == expected_answer.strip():
            return ErrorSeverity.MINOR
        
        # 完全错误（完全不同）
        if len(user_answer) == 0 or len(expected_answer) == 0:
            return ErrorSeverity.SEVERE
        
        # 计算相似度
        similarity = self._calculate_similarity(user_answer, expected_answer)
        
        if similarity > 0.8:
            return ErrorSeverity.MINOR  # 接近正确
        elif similarity > 0.5:
            return ErrorSeverity.MODERATE
        elif similarity > 0.2:
            return ErrorSeverity.SEVERE
        else:
            return ErrorSeverity.CRITICAL
    
    def _calculate_similarity(self, s1: str, s2: str) -> float:
        """
        计算字符串相似度
        """
        if not s1 or not s2:
            return 0.0
        
        s1_set = set(s1.lower())
        s2_set = set(s2.lower())
        
        intersection = len(s1_set & s2_set)
        union = len(s1_set | s2_set)
        
        if union == 0:
            return 0.0
        
        return intersection / union
    
    def _analyze_root_cause(
        self,
        user_answer: str,
        expected_answer: str,
        error_type: ErrorType
    ) -> str:
        """
        分析根本原因
        """
        root_causes = {
            ErrorType.CALCULATION_ERROR: "在计算过程中出现了偏差",
            ErrorType.CONCEPTUAL_ERROR: "对相关概念的理解不够准确",
            ErrorType.LOGICAL_ERROR: "推理过程中的逻辑链条出现问题",
            ErrorType.MISCONCEPTION: "存在对某些概念的误解",
            ErrorType.KNOWLEDGE_GAP: "相关知识还未完全掌握",
            ErrorType.TYPO: "表达时出现了小的失误",
            ErrorType.UNKNOWN: "可能是多种因素共同导致"
        }
        
        return root_causes.get(error_type, root_causes[ErrorType.UNKNOWN])
    
    def _identify_misconceptions(
        self,
        user_answer: str,
        expected_answer: str,
        error_type: ErrorType,
        pattern_matches: List[Tuple[ErrorPattern, float]]
    ) -> List[str]:
        """
        识别相关错误概念
        """
        misconceptions = []
        
        # 从匹配的模式中提取
        for pattern, score in pattern_matches:
            if score > 0.3:
                misconceptions.extend(pattern.common_misconceptions)
        
        # 根据错误类型添加常见误解
        if error_type == ErrorType.CALCULATION_ERROR:
            if '算错' in user_answer or '算' in user_answer:
                misconceptions.append("计算过程中容易出现粗心")
        elif error_type == ErrorType.CONCEPTUAL_ERROR:
            misconceptions.append("对概念的理解停留在表面")
        
        return list(set(misconceptions))[:5]  # 最多5个
    
    def _generate_correction(
        self,
        user_answer: str,
        expected_answer: str,
        error_type: ErrorType,
        pattern_matches: List[Tuple[ErrorPattern, float]]
    ) -> str:
        """
        生成纠正内容
        """
        if pattern_matches and pattern_matches[0][1] > 0.3:
            pattern = pattern_matches[0][0]
            if pattern.correction_hints:
                return pattern.correction_hints[0]
        
        corrections = {
            ErrorType.CALCULATION_ERROR: "重新检查计算过程，注意每一步的运算",
            ErrorType.CONCEPTUAL_ERROR: "建议回顾相关概念的定义和要点",
            ErrorType.LOGICAL_ERROR: "重新梳理推理的逻辑链条",
            ErrorType.MISCONCEPTION: "需要纠正对某些概念的误解",
            ErrorType.KNOWLEDGE_GAP: "建议系统学习相关知识",
            ErrorType.TYPO: "仔细检查表达，注意拼写和格式"
        }
        
        return corrections.get(error_type, "仔细分析答案，找出差异所在")
    
    def _generate_explanation(
        self,
        user_answer: str,
        expected_answer: str,
        error_type: ErrorType,
        root_cause: str
    ) -> str:
        """
        生成错误解释
        """
        explanations = {
            ErrorType.CALCULATION_ERROR: (
                f"你的答案是 {user_answer}，而正确答案是 {expected_answer}。"
                "这可能是在计算过程中的某个步骤出现了偏差。"
            ),
            ErrorType.CONCEPTUAL_ERROR: (
                f"这个问题需要准确理解 '{expected_answer}' 的含义。"
                "建议重新学习相关概念。"
            ),
            ErrorType.LOGICAL_ERROR: (
                "在推理过程中，可能跳过了某些关键步骤或推理方向有误。"
            ),
            ErrorType.MISCONCEPTION: (
                "看来对某些概念的理解有些偏差。"
                "让我们一起来澄清这些概念。"
            )
        }
        
        return explanations.get(
            error_type,
            f"你的答案与正确答案 '{expected_answer}' 有差异。"
        )
    
    def _generate_hints(
        self,
        error_type: ErrorType,
        pattern_matches: List[Tuple[ErrorPattern, float]],
        misconceptions: List[str]
    ) -> List[str]:
        """
        生成提示
        """
        hints = []
        
        # 从模式中提取提示
        for pattern, score in pattern_matches:
            if score > 0.3:
                hints.extend(pattern.correction_hints)
        
        # 添加通用提示
        if error_type == ErrorType.CALCULATION_ERROR:
            hints.append("试着重新做一遍，注意检查每一步")
        elif error_type == ErrorType.CONCEPTUAL_ERROR:
            hints.append("回顾一下相关定义和例子")
        
        return list(set(hints))[:3]  # 最多3个提示
    
    def _extract_related_concepts(
        self,
        pattern_matches: List[Tuple[ErrorPattern, float]]
    ) -> List[str]:
        """
        提取相关概念
        """
        concepts = []
        for pattern, score in pattern_matches:
            if score > 0.3:
                concepts.extend(pattern.related_concepts)
        return list(set(concepts))[:5]
    
    def _calculate_confidence(
        self,
        error_type: ErrorType,
        severity: ErrorSeverity,
        pattern_matches: List[Tuple[ErrorPattern, float]]
    ) -> float:
        """
        计算分析置信度
        """
        confidence = 0.5  # 基础置信度
        
        # 模式匹配加成
        if pattern_matches and pattern_matches[0][1] > 0.5:
            confidence += 0.3
        elif pattern_matches:
            confidence += 0.1
        
        # 严重程度影响
        if severity == ErrorSeverity.SEVERE:
            confidence += 0.1
        
        # 错误类型明确性
        if error_type != ErrorType.UNKNOWN:
            confidence += 0.1
        
        return min(confidence, 1.0)
    
    def _generate_error_id(self) -> str:
        """生成错误ID"""
        import hashlib
        import time
        raw = f"{time.time()}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]
    
    def _describe_error(self, error_type: ErrorType, severity: ErrorSeverity) -> str:
        """描述错误"""
        descriptions = {
            ErrorType.CALCULATION_ERROR: "计算结果有误",
            ErrorType.CONCEPTUAL_ERROR: "概念理解不正确",
            ErrorType.LOGICAL_ERROR: "推理过程有问题",
            ErrorType.MISCONCEPTION: "存在概念误解",
            ErrorType.KNOWLEDGE_GAP: "知识掌握不足",
            ErrorType.TYPO: "表达有误",
            ErrorType.UNKNOWN: "答案不正确"
        }
        
        severity_text = {
            ErrorSeverity.MINOR: "轻微",
            ErrorSeverity.MODERATE: "中等",
            ErrorSeverity.SEVERE: "严重",
            ErrorSeverity.CRITICAL: "关键"
        }
        
        return f"[{severity_text[severity]}{descriptions.get(error_type, '错误')}]"


class AdaptiveFeedbackGenerator:
    """
    自适应反馈生成器
    
    根据用户状态和错误类型生成个性化反馈
    """
    
    # 反馈模板
    EMPATHETIC_INTROS = {
        ErrorSeverity.MINOR: [
            "没关系，这是很常见的小问题！",
            "差一点就对了，继续加油！"
        ],
        ErrorSeverity.MODERATE: [
            "这道题有点难度，别灰心！",
            "很多人在这里都会遇到困难，一起看看吧。"
        ],
        ErrorSeverity.SEVERE: [
            "这个错误很常见，说明你正在挑战自己！",
            "别担心，我们一起来分析一下。"
        ],
        ErrorSeverity.CRITICAL: [
            "看起来这里需要一些额外的关注。",
            "让我们仔细看看这个问题。"
        ]
    }
    
    ENCOURAGEMENTS = [
        "错误是最好的老师，你正在进步！",
        "每一次纠错都是成长的机会。",
        "坚持就是胜利，你一定能掌握的！",
        "学习就是一个不断试错的过程。",
        "继续保持这个探索精神！"
    ]
    
    def __init__(self, llm_client=None):
        """
        初始化反馈生成器
        
        Args:
            llm_client: LLM客户端
        """
        self.llm_client = llm_client
    
    def generate_feedback(
        self,
        error: DetectedError,
        user_profile: Optional[Dict] = None,
        engagement_level: Optional[float] = None,
        emotion_state: Optional[str] = None
    ) -> CorrectionFeedback:
        """
        生成自适应反馈
        
        Args:
            error: 检测到的错误
            user_profile: 用户画像
            engagement_level: 参与度
            emotion_state: 当前情感状态
            
        Returns:
            CorrectionFeedback: 反馈内容
        """
        # 确定反馈级别
        feedback_level = self._determine_feedback_level(
            error, user_profile, engagement_level
        )
        
        # 生成同理心引导
        empathetic_intro = self._generate_empathetic_intro(
            error, emotion_state
        )
        
        # 生成错误解释
        error_explanation = error.explanation
        
        # 生成正确答案
        correct_answer = error.expected_answer
        
        # 生成解题步骤
        steps = self._generate_solution_steps(error)
        
        # 生成后续建议
        related_concepts = error.related_concepts
        practice_recs = self._generate_practice_recommendations(error)
        learning_path = self._generate_learning_path_suggestions(error)
        
        # 生成鼓励
        encouragement = self._generate_encouragement(error, emotion_state)
        
        return CorrectionFeedback(
            error=error,
            feedback_level=feedback_level,
            empathetic_intro=empathetic_intro,
            error_explanation=error_explanation,
            correct_answer=correct_answer,
            step_by_step_solution=steps,
            related_concepts_to_review=related_concepts,
            practice_recommendations=practice_recs,
            learning_path_suggestions=learning_path,
            encouragement=encouragement
        )
    
    def _determine_feedback_level(
        self,
        error: DetectedError,
        user_profile: Optional[Dict],
        engagement_level: Optional[float]
    ) -> str:
        """
        确定反馈详细程度
        """
        # 高参与度用户给详细反馈
        if engagement_level and engagement_level > 0.7:
            return "detailed"
        
        # 高置信度的错误给详细反馈
        if error.confidence > 0.8:
            return "detailed"
        
        # 低置信度给中等反馈
        if error.confidence < 0.5:
            return "moderate"
        
        # 严重错误给详细反馈
        if error.severity in [ErrorSeverity.SEVERE, ErrorSeverity.CRITICAL]:
            return "detailed"
        
        return "moderate"
    
    def _generate_empathetic_intro(
        self,
        error: DetectedError,
        emotion_state: Optional[str]
    ) -> str:
        """
        生成同理心引导
        """
        intros = self.EMPATHETIC_INTROS.get(error.severity, self.EMPATHETIC_INTROS[ErrorSeverity.MODERATE])
        
        # 根据情感状态调整
        if emotion_state == 'frustrated':
            return "我能理解你的挫败感，让我们一起慢慢来。"
        elif emotion_state == 'anxious':
            return "别着急，我们一步一步来分析。"
        
        return intros[0]
    
    def _generate_solution_steps(
        self,
        error: DetectedError
    ) -> List[str]:
        """
        生成分步解答
        """
        # 根据错误类型生成不同的步骤
        if error.error_type == ErrorType.CALCULATION_ERROR:
            return [
                "第一步：仔细分析题目要求",
                "第二步：理清已知条件和求解目标",
                "第三步：按照正确的方法逐步计算",
                "第四步：检查计算过程中的每一步",
                "第五步：验证最终答案"
            ]
        elif error.error_type == ErrorType.CONCEPTUAL_ERROR:
            return [
                "首先，理解这个概念的核心定义",
                "其次，区分容易混淆的相关概念",
                "然后，通过具体例子加深理解",
                "最后，做一些练习巩固认识"
            ]
        elif error.error_type == ErrorType.LOGICAL_ERROR:
            return [
                "第一步：明确前提条件",
                "第二步：梳理推理的逻辑链条",
                "第三步：检查每个推理步骤是否正确",
                "第四步：得出结论并验证"
            ]
        
        return [
            "仔细分析题目要求",
            "回顾相关知识点",
            "按照正确方法求解",
            "验证答案的正确性"
        ]
    
    def _generate_practice_recommendations(
        self,
        error: DetectedError
    ) -> List[str]:
        """
        生成练习建议
        """
        recs = []
        
        # 根据错误类型建议
        if error.error_type == ErrorType.CALCULATION_ERROR:
            recs.append("多做一些计算练习，提高计算的准确性")
            recs.append("练习心算和笔算的结合")
        elif error.error_type == ErrorType.CONCEPTUAL_ERROR:
            recs.append("复习相关概念的定义和例子")
            recs.append("尝试用自己的话解释这个概念")
        elif error.error_type == ErrorType.LOGICAL_ERROR:
            recs.append("多做一些逻辑推理练习")
            recs.append("学习一些基本的逻辑推理方法")
        
        # 添加通用建议
        if not recs:
            recs.append("针对这个类型的题目多加练习")
        
        return recs
    
    def _generate_learning_path_suggestions(
        self,
        error: DetectedError
    ) -> List[str]:
        """
        生成学习路径建议
        """
        suggestions = []
        
        if error.related_concepts:
            concepts = "、".join(error.related_concepts[:3])
            suggestions.append(f"建议先学习：{concepts}")
        
        if error.error_type == ErrorType.CONCEPTUAL_ERROR:
            suggestions.append("建议从基础概念开始系统学习")
        elif error.error_type == ErrorType.KNOWLEDGE_GAP:
            suggestions.append("建议回顾之前学过的相关知识")
        
        return suggestions
    
    def _generate_encouragement(
        self,
        error: DetectedError,
        emotion_state: Optional[str]
    ) -> str:
        """
        生成鼓励信息
        """
        # 根据情感状态调整
        if emotion_state == 'frustrated':
            return "你之前也解决过很多难题，相信这次也可以的！"
        elif emotion_state == 'anxious':
            return "慢慢来，你比自己想象的更有能力！"
        elif emotion_state == 'tired':
            return "休息一下再继续，效率最重要！"
        
        # 根据错误严重程度
        if error.severity == ErrorSeverity.MINOR:
            return "很好，就差一点点了！"
        elif error.severity == ErrorSeverity.SEVERE:
            return "这个挑战会让你的理解更加深刻！"
        
        return self.ENCOURAGEMENTS[0]


class ErrorCorrectionEngine:
    """
    错题检测与纠正引擎 - 论文核心实现
    
    整合错误检测、分类、原因分析和智能纠正
    """
    
    def __init__(self, llm_client=None):
        """
        初始化纠正引擎
        
        Args:
            llm_client: LLM客户端
        """
        self.analyzer = ErrorAnalyzer(llm_client)
        self.feedback_generator = AdaptiveFeedbackGenerator(llm_client)
        
        # 错题记录: {user_id: [DetectedError]}
        self.error_history: Dict[int, List[DetectedError]] = defaultdict(list)
        
        # 用户错误统计: {user_id: {error_type: count}}
        self.user_error_stats: Dict[int, Dict[ErrorType, int]] = defaultdict(lambda: defaultdict(int))
    
    def detect_and_correct(
        self,
        user_id: int,
        user_answer: str,
        expected_answer: str,
        question: str,
        context: Optional[Dict] = None,
        user_profile: Optional[Dict] = None
    ) -> CorrectionFeedback:
        """
        检测错误并生成纠正反馈
        
        Args:
            user_id: 用户ID
            user_answer: 用户答案
            expected_answer: 期望答案
            question: 问题
            context: 上下文
            user_profile: 用户画像
            
        Returns:
            CorrectionFeedback: 纠正反馈
        """
        # 1. 分析错误
        error = self.analyzer.analyze_error(
            user_answer=user_answer,
            expected_answer=expected_answer,
            question=question,
            context=context
        )
        
        # 2. 记录错误历史
        self.error_history[user_id].append(error)
        
        # 3. 更新错误统计
        self.user_error_stats[user_id][error.error_type] += 1
        
        # 4. 获取用户状态
        engagement_level = None
        emotion_state = None
        if user_profile:
            engagement_level = user_profile.get('engagement_level')
            emotion_state = user_profile.get('emotion_state')
        
        # 5. 生成自适应反馈
        feedback = self.feedback_generator.generate_feedback(
            error=error,
            user_profile=user_profile,
            engagement_level=engagement_level,
            emotion_state=emotion_state
        )
        
        return feedback
    
    def batch_detect(
        self,
        user_id: int,
        answers: List[Dict],
        user_profile: Optional[Dict] = None
    ) -> List[CorrectionFeedback]:
        """
        批量检测错误
        
        Args:
            user_id: 用户ID
            answers: 答案列表 [{question, user_answer, expected_answer}, ...]
            user_profile: 用户画像
            
        Returns:
            纠正反馈列表
        """
        feedbacks = []
        
        for answer_item in answers:
            feedback = self.detect_and_correct(
                user_id=user_id,
                user_answer=answer_item.get('user_answer', ''),
                expected_answer=answer_item.get('expected_answer', ''),
                question=answer_item.get('question', ''),
                context=answer_item.get('context'),
                user_profile=user_profile
            )
            feedbacks.append(feedback)
        
        return feedbacks
    
    def get_error_analysis(
        self,
        user_id: int,
        time_range_days: Optional[int] = None
    ) -> Dict:
        """
        获取错误分析报告
        
        Args:
            user_id: 用户ID
            time_range_days: 时间范围（天）
            
        Returns:
            错误分析报告
        """
        errors = self.error_history.get(user_id, [])
        
        # 时间过滤
        if time_range_days:
            cutoff = datetime.now() - timedelta(days=time_range_days)
            errors = [e for e in errors if e.timestamp >= cutoff]
        
        if not errors:
            return {
                'total_errors': 0,
                'error_distribution': {},
                'most_common_errors': [],
                'improvement_trend': 'no_data'
            }
        
        # 错误类型分布
        error_distribution = Counter(e.error_type for e in errors)
        
        # 最常见错误
        common_errors = [
            {
                'error_type': et.value,
                'count': count,
                'severity': self._get_most_severe(errors, et).value if errors else 'minor'
            }
            for et, count in error_distribution.most_common(5)
        ]
        
        # 改进趋势
        improvement_trend = self._calculate_improvement_trend(errors)
        
        return {
            'total_errors': len(errors),
            'error_distribution': {et.value: count for et, count in error_distribution.items()},
            'most_common_errors': common_errors,
            'improvement_trend': improvement_trend,
            'average_confidence': sum(e.confidence for e in errors) / len(errors)
        }
    
    def _get_most_severe(
        self,
        errors: List[DetectedError],
        error_type: ErrorType
    ) -> ErrorSeverity:
        """获取最严重的错误级别"""
        type_errors = [e for e in errors if e.error_type == error_type]
        if type_errors:
            return max(type_errors, key=lambda e: e.severity.value).severity
        return ErrorSeverity.MINOR
    
    def _calculate_improvement_trend(
        self,
        errors: List[DetectedError]
    ) -> str:
        """
        计算改进趋势
        """
        if len(errors) < 5:
            return 'insufficient_data'
        
        # 分为前后两半
        mid = len(errors) // 2
        first_half = errors[:mid]
        second_half = errors[mid:]
        
        # 计算平均严重程度
        first_avg = sum(e.severity.value for e in first_half) / len(first_half)
        second_avg = sum(e.severity.value for e in second_half) / len(second_half)
        
        if second_avg < first_avg - 0.3:
            return 'improving'
        elif second_avg > first_avg + 0.3:
            return 'declining'
        else:
            return 'stable'
    
    def get_common_misconceptions(
        self,
        user_id: int,
        limit: int = 10
    ) -> List[Dict]:
        """
        获取常见错误概念
        
        Args:
            user_id: 用户ID
            limit: 返回数量
            
        Returns:
            常见错误概念列表
        """
        errors = self.error_history.get(user_id, [])
        
        # 收集所有误解
        all_misconceptions = []
        for error in errors:
            for misconception in error.misconceptions:
                all_misconceptions.append({
                    'misconception': misconception,
                    'error_type': error.error_type.value,
                    'severity': error.severity.value,
                    'timestamp': error.timestamp.isoformat()
                })
        
        # 统计频率
        misconception_counts = Counter(m['misconception'] for m in all_misconceptions)
        
        # 按频率排序
        sorted_misconceptions = [
            {
                'misconception': misconception,
                'count': count,
                'details': [m for m in all_misconceptions if m['misconception'] == misconception]
            }
            for misconception, count in misconception_counts.most_common(limit)
        ]
        
        return sorted_misconceptions
    
    def suggest_review_topics(
        self,
        user_id: int,
        limit: int = 5
    ) -> List[str]:
        """
        建议复习主题
        
        基于用户的错误历史，推荐需要复习的知识点
        
        Args:
            user_id: 用户ID
            limit: 返回数量
            
        Returns:
            复习主题列表
        """
        errors = self.error_history.get(user_id, [])
        
        # 收集相关概念
        concepts = []
        for error in errors:
            concepts.extend(error.related_concepts)
        
        # 统计频率
        concept_counts = Counter(concepts)
        
        # 返回最需要复习的概念
        return [concept for concept, _ in concept_counts.most_common(limit)]


# 全局错题纠正引擎实例
_error_correction_engine: Optional[ErrorCorrectionEngine] = None


def get_error_correction_engine(llm_client=None) -> ErrorCorrectionEngine:
    """
    获取全局错题纠正引擎实例
    
    Args:
        llm_client: LLM客户端
        
    Returns:
        ErrorCorrectionEngine实例
    """
    global _error_correction_engine
    if _error_correction_engine is None:
        _error_correction_engine = ErrorCorrectionEngine(llm_client)
    return _error_correction_engine


def detect_and_correct_answer(
    user_id: int,
    user_answer: str,
    expected_answer: str,
    question: str,
    context: Optional[Dict] = None
) -> Dict:
    """
    检测并纠正答案的便捷函数
    """
    engine = get_error_correction_engine()
    feedback = engine.detect_and_correct(
        user_id=user_id,
        user_answer=user_answer,
        expected_answer=expected_answer,
        question=question,
        context=context
    )
    return feedback.to_user_message()
