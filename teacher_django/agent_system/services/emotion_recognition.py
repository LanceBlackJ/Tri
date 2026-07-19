"""
情感智能模块 - 基于论文 arXiv:2505.19803v2

论文核心实现：
- OCC (Ortony, Clore, and Collins) 情感模型
- 并行同理心 (Parallel Empathy) / 反应性同理心 (Reactive Empathy)
- 情感识别与响应
- 情感历史追踪
"""

import re
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict

logger = logging.getLogger(__name__)


class EmotionType(Enum):
    """
    OCC情感模型中的24种基本情感类型
    基于Ortony, Clore, and Collins的情感分类
    """
    # 正面情感 - 高兴相关
    JOY = "joy"                    # 喜悦
    HAPPINESS = "happiness"         # 幸福
    SATISFACTION = "satisfaction"  # 满足
    RELIEF = "relief"              # 放松
    PRIDE = "pride"               # 自豪
    ADMIRATION = "admiration"     # 钦佩
    LOVE = "love"                 # 爱
    HOPE = "hope"                 # 希望
    CURIOSITY = "curiosity"       # 好奇
    
    # 负面情感 - 不高兴相关
    DISTRESS = "distress"         # 悲伤
    SADNESS = "sadness"           # 难过
    DISAPPOINTMENT = "disappointment"  # 失望
    REMORSE = "remorse"           # 后悔
    SHAME = "shame"              # 羞耻
    GUILT = "guilt"              # 内疚
    FEAR = "fear"                # 恐惧
    FEAR_CONFETTION = "fear_confirmed"  # 恐惧确认
    DISPproval = "disapproval"   # 反对
    REPROACH = "reproach"        # 责备
    ANGER = "anger"              # 愤怒
    HATRED = "hatred"            # 憎恨
    GLOATING = "gloating"        # 幸灾乐祸
    CONFUSION = "confusion"       # 困惑
    FRUSTRATION = "frustration"   # 挫败
    ANXIETY = "anxiety"           # 焦虑
    BOREDOM = "boredom"           # 无聊
    
    # 复合情感
    GRATIFICATION = "gratification"     # 满足感
    SORROW = "sorrow"                   # 悲痛
    JOY_IN_THE_TRORBUNE_OF_ANOTHER = "joy_in_another"  # 为他人高兴
    RESENTMENT = "resentment"          # 怨恨


class EmotionalState(Enum):
    """
    教育场景中的情感状态分类
    用于简化的情感识别
    """
    ENGAGED = "engaged"           # 投入
    CURIOUS = "curious"           # 好奇
    CONFUSED = "confused"         # 困惑
    FRUSTRATED = "frustrated"    # 挫败
    ANXIOUS = "anxious"           # 焦虑
    BORED = "bored"              # 厌倦
    EXCITED = "excited"           # 兴奋
    CONFIDENT = "confident"       # 自信
    UNCERTAIN = "uncertain"      # 不确定
    SATISFIED = "satisfied"       # 满意
    ANGRY = "angry"              # 生气
    OVERWHELMED = "overwhelmed"  # 不知所措
    MOTIVATED = "motivated"       # 有动力
    TIRED = "tired"              # 疲劳


class EmpathyStrategy(Enum):
    """
    同理心响应策略
    """
    PARALLEL_EMPATHY = "parallel"    # 并行同理心：与用户情感同步
    REACTIVE_EMPATHY = "reactive"    # 反应性同理心：回应用户情感
    COGNITIVE_EMPATHY = "cognitive"  # 认知同理心：理解并解释情感
    BEHAVIORAL_EMPATHY = "behavioral"  # 行为同理心：采取行动帮助


@dataclass
class EmotionData:
    """
    情感数据结构
    """
    emotion_type: EmotionType
    state: EmotionalState
    intensity: float = 0.0  # 0.0 - 1.0
    confidence: float = 0.0  # 0.0 - 1.0
    triggers: List[str] = field(default_factory=list)  # 触发词/原因
    timestamp: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict:
        return {
            'emotion_type': self.emotion_type.value,
            'state': self.state.value,
            'intensity': self.intensity,
            'confidence': self.confidence,
            'triggers': self.triggers,
            'timestamp': self.timestamp.isoformat()
        }


@dataclass
class EmotionHistory:
    """
    情感历史记录
    """
    user_id: int
    session_id: Optional[int] = None
    emotions: List[EmotionData] = field(default_factory=list)
    emotional_shifts: List[Dict] = field(default_factory=list)  # 情感转变记录
    dominant_emotion: Optional[EmotionalState] = None
    emotional_valence: float = 0.0  # -1.0 (负面) to 1.0 (正面)
    arousal_level: float = 0.0  # 0.0 (平静) to 1.0 (激动)
    
    def add_emotion(self, emotion: EmotionData):
        """添加情感记录"""
        self.emotions.append(emotion)
        self._update_statistics()
    
    def _update_statistics(self):
        """更新统计信息"""
        if not self.emotions:
            return
        
        # 计算主导情感
        emotion_counts = defaultdict(int)
        for e in self.emotions:
            emotion_counts[e.state] += e.intensity
        
        if emotion_counts:
            self.dominant_emotion = max(emotion_counts, key=emotion_counts.get)
        
        # 计算情感效价 (Valence) 和唤醒度 (Arousal)
        valence_map = {
            EmotionalState.ENGAGED: 0.5,
            EmotionalState.CURIOUS: 0.6,
            EmotionalState.CONFUSED: -0.4,
            EmotionalState.FRUSTRATED: -0.7,
            EmotionalState.ANXIOUS: -0.5,
            EmotionalState.BORED: -0.3,
            EmotionalState.EXCITED: 0.8,
            EmotionalState.CONFIDENT: 0.7,
            EmotionalState.UNCERTAIN: -0.2,
            EmotionalState.SATISFIED: 0.8,
            EmotionalState.ANGRY: -0.8,
            EmotionalState.OVERWHELMED: -0.6,
            EmotionalState.MOTIVATED: 0.7,
            EmotionalState.TIRED: -0.4,
        }
        
        total_valence = sum(
            valence_map.get(e.state, 0) * e.intensity 
            for e in self.emotions
        )
        total_intensity = sum(e.intensity for e in self.emotions)
        
        if total_intensity > 0:
            self.emotional_valence = total_valence / total_intensity
            self.arousal_level = total_intensity / len(self.emotions)
    
    def detect_emotional_shift(self, previous: Optional[EmotionData], current: EmotionData) -> bool:
        """
        检测情感转变
        Returns: 是否有显著情感转变
        """
        if not previous:
            return False
        
        valence_change = abs(current.intensity - previous.intensity) > 0.3
        
        if valence_change:
            self.emotional_shifts.append({
                'from': previous.state.value,
                'to': current.state.value,
                'timestamp': datetime.now().isoformat(),
                'magnitude': abs(current.intensity - previous.intensity)
            })
            return True
        return False
    
    def to_dict(self) -> Dict:
        return {
            'user_id': self.user_id,
            'session_id': self.session_id,
            'emotions': [e.to_dict() for e in self.emotions],
            'emotional_shifts': self.emotional_shifts,
            'dominant_emotion': self.dominant_emotion.value if self.dominant_emotion else None,
            'emotional_valence': self.emotional_valence,
            'arousal_level': self.arousal_level
        }


class OCNEmotionModel:
    """
    OCC (Ortony, Clore, and Collins) 情感模型实现
    
    基于论文: Integrating emotional intelligence, memory architecture, and gestures
    实现情感识别、分类和强度计算
    """
    
    # 情感词汇词典 - 英文原文用于匹配，中文用于理解
    EMOTION_LEXICON = {
        # 高兴/喜悦类
        'joy': {'happy', 'joy', 'glad', 'pleased', 'delighted', 'thrilled', 'excited', '太好了', '开心', '高兴', '真棒', '厉害', '太好了'},
        'satisfaction': {'satisfied', 'content', 'fulfilled', 'accomplished', '满意', '满足', '完成了', '搞定了'},
        'pride': {'proud', 'accomplished', 'impressive', '自豪', '骄傲', '厉害', '佩服'},
        'hope': {'hope', 'hopeful', 'optimistic', '期待', '希望', '应该可以', '也许'},
        'excitement': {'excited', 'thrilled', 'enthusiastic', '兴奋', '激动', '太棒了', '好期待'},
        
        # 不高兴/负面类
        'confusion': {'confused', 'unclear', 'puzzled', 'dont understand', "can't understand", '困惑', '不懂', '不明白', '怎么回事', '什么意思', '搞不懂', '晕'},
        'frustration': {'frustrated', 'annoyed', 'irritated', 'stuck', '难住', '挫败', '烦', '气', '郁闷', '无语'},
        'anxiety': {'anxious', 'worried', 'nervous', 'concerned', '紧张', '担心', '焦虑', '害怕', '慌'},
        'boredom': {'bored', 'tedious', 'boring', '单调', '无聊', '没意思', '烦'},
        'anger': {'angry', 'mad', 'furious', 'annoyed', '生气', '愤怒', '恼火'},
        'disappointment': {'disappointed', 'let down', 'sad', '失望', '难过', '遗憾'},
        'overwhelm': {'overwhelmed', 'stressed', 'too much', '压力大', '太多', '应付不来', '崩溃'},
        'tiredness': {'tired', 'exhausted', 'sleepy', '累', '困', '疲劳'},
        
        # 不确定类
        'uncertainty': {'maybe', 'perhaps', 'not sure', 'uncertain', 'maybe', 'perhaps', 'perhaps', '可能', '也许', '不确定', '不太确定', '应该'},
        'curiosity': {'curious', 'wonder', 'interested', '想知道', '好奇', '有意思', '有趣'},
        
        # 自信类
        'confidence': {'confident', 'sure', 'certain', 'know', '相信', '确定', '肯定', '没问题'},
        'engagement': {'great', 'good', 'nice', 'like', 'enjoy', '好', '不错', '喜欢', '挺好'},
    }
    
    # 情感强度修饰词
    INTENSITY_MODIFIERS = {
        'very': 1.5, 'really': 1.5, 'extremely': 1.8, 'super': 1.6,
        'quite': 1.2, 'pretty': 1.2, 'somewhat': 0.7, 'a bit': 0.5,
        'slightly': 0.4, 'kind of': 0.5, 'sort of': 0.5,
        '太': 1.5, '非常': 1.5, '特别': 1.5, '相当': 1.3,
        '有点': 0.6, '有点': 0.6, '稍微': 0.5, '一点': 0.4,
    }
    
    # 否定词
    NEGATION_WORDS = {'not', 'no', "don't", "doesn't", "isn't", "aren't", "wasn't", "weren't",
                      '不', '没', '无', '非', '别', '不是', '没有'}
    
    def __init__(self):
        self._build_emotion_patterns()
    
    def _build_emotion_patterns(self):
        """构建情感匹配模式"""
        self.emotion_patterns = {}
        
        for emotion_key, keywords in self.EMOTION_LEXICON.items():
            # 构建正则表达式模式
            pattern = r'\b(' + '|'.join(re.escape(k) for k in keywords) + r')\b'
            self.emotion_patterns[emotion_key] = re.compile(pattern, re.IGNORECASE)
    
    def detect_emotion_from_text(self, text: str) -> List[Dict[str, Any]]:
        """
        从文本中检测情感
        
        Args:
            text: 用户输入文本
            
        Returns:
            检测到的情感列表，包含类型、强度、置信度等信息
        """
        text_lower = text.lower()
        detected_emotions = []
        
        for emotion_key, pattern in self.emotion_patterns.items():
            matches = pattern.findall(text_lower)
            if matches:
                # 计算基础强度
                base_intensity = min(len(matches) * 0.2, 1.0)
                
                # 检查强度修饰词
                intensity_multiplier = 1.0
                for modifier, mult in self.INTENSITY_MODIFIERS.items():
                    if modifier in text_lower:
                        intensity_multiplier = mult
                        break
                
                # 检查否定词
                has_negation = any(neg in text_lower for neg in self.NEGATION_WORDS)
                
                # 计算最终强度
                final_intensity = base_intensity * intensity_multiplier
                if has_negation:
                    final_intensity *= 0.5
                
                final_intensity = min(max(final_intensity, 0.0), 1.0)
                
                # 计算置信度 (基于匹配数量)
                confidence = min(0.5 + len(matches) * 0.1, 1.0)
                
                detected_emotions.append({
                    'emotion_key': emotion_key,
                    'matches': matches,
                    'intensity': final_intensity,
                    'confidence': confidence,
                    'has_negation': has_negation
                })
        
        return detected_emotions
    
    def map_to_emotional_state(self, emotion_key: str) -> EmotionalState:
        """
        将OCC情感映射到简化的情感状态
        
        Args:
            emotion_key: OCC情感类型键
            
        Returns:
            对应的EmotionalState
        """
        mapping = {
            'joy': EmotionalState.EXCITED,
            'satisfaction': EmotionalState.SATISFIED,
            'pride': EmotionalState.CONFIDENT,
            'hope': EmotionalState.MOTIVATED,
            'excitement': EmotionalState.EXCITED,
            'confusion': EmotionalState.CONFUSED,
            'frustration': EmotionalState.FRUSTRATED,
            'anxiety': EmotionalState.ANXIOUS,
            'boredom': EmotionalState.BORED,
            'anger': EmotionalState.ANGRY,
            'disappointment': EmotionalState.UNCERTAIN,
            'overwhelm': EmotionalState.OVERWHELMED,
            'tiredness': EmotionalState.TIRED,
            'uncertainty': EmotionalState.UNCERTAIN,
            'curiosity': EmotionalState.CURIOUS,
            'confidence': EmotionalState.CONFIDENT,
            'engagement': EmotionalState.ENGAGED,
        }
        return mapping.get(emotion_key, EmotionalState.ENGAGED)
    
    def calculate_emotion_intensity(self, emotion_key: str, context: Dict) -> float:
        """
        计算情感强度
        
        基于多个因素：
        1. 情感词汇的出现次数
        2. 强度修饰词
        3. 上下文信息（是否在学习困难内容等）
        
        Args:
            emotion_key: 情感类型键
            context: 上下文信息
            
        Returns:
            情感强度 (0.0 - 1.0)
        """
        base_intensity = context.get('base_intensity', 0.5)
        
        # 上下文调整
        if context.get('is_learning_struggle'):
            # 学习困难场景，增强困惑/挫败感的强度
            if emotion_key in ['confusion', 'frustration']:
                base_intensity *= 1.3
        
        if context.get('is_success'):
            # 成功场景，增强正面情感的强度
            if emotion_key in ['joy', 'satisfaction', 'pride']:
                base_intensity *= 1.3
        
        if context.get('repeated_attempt'):
            # 重复尝试场景
            if emotion_key in ['frustration', 'anxiety']:
                base_intensity *= 1.2
        
        return min(base_intensity, 1.0)
    
    def infer_emotions_from_behavior(self, user_behavior: Dict) -> List[Dict]:
        """
        从用户行为中推断情感状态
        
        基于论文中的行为指标推断情感
        
        Args:
            user_behavior: 用户行为数据，包含：
                - response_time: 响应时间
                - error_rate: 错误率
                - help_requests: 求助次数
                - engagement_level: 参与度
                - task_completion: 任务完成情况
                
        Returns:
            推断的情感列表
        """
        inferred_emotions = []
        
        # 检查行为数据是否为空
        if user_behavior is None:
            user_behavior = {}
        
        # 响应时间分析
        response_time = user_behavior.get('response_time', 0)
        if response_time > 120:  # 超过2分钟
            inferred_emotions.append({
                'emotion_key': 'frustration',
                'intensity': 0.6,
                'confidence': 0.7,
                'reason': 'response_time_high'
            })
        elif response_time < 5:  # 快速响应
            inferred_emotions.append({
                'emotion_key': 'engagement',
                'intensity': 0.5,
                'confidence': 0.6,
                'reason': 'quick_response'
            })
        
        # 错误率分析
        error_rate = user_behavior.get('error_rate', 0)
        if error_rate > 0.5:
            inferred_emotions.append({
                'emotion_key': 'frustration',
                'intensity': error_rate,
                'confidence': 0.8,
                'reason': 'high_error_rate'
            })
        elif error_rate == 0 and user_behavior.get('task_completion', 0) > 0:
            inferred_emotions.append({
                'emotion_key': 'satisfaction',
                'intensity': 0.7,
                'confidence': 0.7,
                'reason': 'no_errors'
            })
        
        # 求助行为分析
        help_requests = user_behavior.get('help_requests', 0)
        if help_requests > 3:
            inferred_emotions.append({
                'emotion_key': 'confusion',
                'intensity': min(help_requests * 0.2, 1.0),
                'confidence': 0.6,
                'reason': 'frequent_help_requests'
            })
        
        # 参与度分析
        engagement = user_behavior.get('engagement_level', 0.5)
        if engagement < 0.3:
            inferred_emotions.append({
                'emotion_key': 'boredom',
                'intensity': 1 - engagement,
                'confidence': 0.5,
                'reason': 'low_engagement'
            })
        elif engagement > 0.8:
            inferred_emotions.append({
                'emotion_key': 'excitement',
                'intensity': engagement,
                'confidence': 0.6,
                'reason': 'high_engagement'
            })
        
        return inferred_emotions


class EmotionRecognitionEngine:
    """
    情感识别引擎 - 论文核心实现
    
    功能：
    1. 文本情感识别 (基于OCC模型)
    2. 行为情感推断
    3. 情感历史追踪
    4. 情感趋势分析
    5. 情感预测
    """
    
    def __init__(self, llm_client=None):
        """
        初始化情感识别引擎
        
        Args:
            llm_client: 大模型客户端，用于深度情感分析
        """
        self.occ_model = OCNEmotionModel()
        self.llm_client = llm_client
        
        # 情感历史存储 {user_id: EmotionHistory}
        self.emotion_histories: Dict[int, EmotionHistory] = {}
        
        # 教育场景特定的情感模式
        self.educational_emotion_patterns = self._build_educational_patterns()
    
    def _build_educational_patterns(self) -> Dict:
        """
        构建教育场景特定的情感模式
        
        Returns:
            教育情感模式字典
        """
        return {
            # 学习困难模式
            'learning_struggle': {
                'patterns': [
                    r'不会做', r'做不出来', r'不知道怎么做', r'看不懂',
                    r"can't do", r"don't know how", r"stuck",
                    r"too hard", r"too difficult", r"don't understand"
                ],
                'emotions': [EmotionalState.CONFUSED, EmotionalState.FRUSTRATED],
                'urgency': 'medium'
            },
            # 取得进步模式
            'progress': {
                'patterns': [
                    r'懂了', r'明白了', r'原来如此', r'理解了',
                    r'明白了', r"get it", r"understand now", r"makes sense"
                ],
                'emotions': [EmotionalState.SATISFIED, EmotionalState.CONFIDENT],
                'urgency': 'low'
            },
            # 需要帮助模式
            'seeking_help': {
                'patterns': [
                    r'帮我', r'教我', r'解释一下', r'什么意思',
                    r'help me', r'teach me', r'explains', r'what is'
                ],
                'emotions': [EmotionalState.CURIOUS, EmotionalState.UNCERTAIN],
                'urgency': 'high'
            },
            # 失去兴趣模式
            'lost_interest': {
                'patterns': [
                    r'无聊', r'没意思', r'不感兴趣', r'不想学',
                    r'boring', r"not interested", r"don't want to"
                ],
                'emotions': [EmotionalState.BORED, EmotionalState.MOTIVATED],
                'urgency': 'medium'
            },
            # 焦虑压力模式
            'stress': {
                'patterns': [
                    r'考试', r'来不及', r'压力', r'担心',
                    r'exam', r"running out of time", r"worried", r"stress"
                ],
                'emotions': [EmotionalState.ANXIOUS, EmotionalState.OVERWHELMED],
                'urgency': 'high'
            },
            # 积极主动模式
            'proactive': {
                'patterns': [
                    r'我想学', r'我想了解', r'还有呢', r'然后呢',
                    r'I want to learn', r'tell me more', r'what else', r"let's continue"
                ],
                'emotions': [EmotionalState.MOTIVATED, EmotionalState.CURIOUS],
                'urgency': 'low'
            }
        }
    
    def recognize_emotion(self, user_id: int, text: str, context: Optional[Dict] = None) -> EmotionData:
        """
        识别用户情感
        
        主要方法，整合多种情感识别策略
        
        Args:
            user_id: 用户ID
            text: 用户输入文本
            context: 上下文信息
            
        Returns:
            EmotionData: 识别出的情感数据
        """
        context = context or {}
        
        # 1. 基于文本的情感识别
        text_emotions = self.occ_model.detect_emotion_from_text(text)
        
        # 2. 基于教育场景模式的识别
        pattern_emotions = self._recognize_educational_patterns(text)
        
        # 3. 基于行为的情感推断
        behavior_emotions = []
        if 'behavior_data' in context:
            behavior_emotions = self.occ_model.infer_emotions_from_behavior(context['behavior_data'])
        
        # 4. 融合多种识别结果
        dominant_emotion = self._fuse_emotions(text_emotions, pattern_emotions, behavior_emotions)
        
        # 5. 创建情感数据
        emotion_data = EmotionData(
            emotion_type=self._get_occ_type(dominant_emotion['state']),
            state=dominant_emotion['state'],
            intensity=dominant_emotion['intensity'],
            confidence=dominant_emotion['confidence'],
            triggers=dominant_emotion.get('triggers', []),
            timestamp=datetime.now()
        )
        
        # 6. 更新情感历史
        self._update_emotion_history(user_id, emotion_data, context.get('session_id'))
        
        return emotion_data
    
    def _recognize_educational_patterns(self, text: str) -> List[Dict]:
        """
        识别教育场景特定的情感模式
        
        Args:
            text: 用户输入文本
            
        Returns:
            匹配到的模式列表
        """
        matched_patterns = []
        text_lower = text.lower()
        
        for pattern_name, pattern_info in self.educational_emotion_patterns.items():
            for pattern in pattern_info['patterns']:
                if re.search(pattern, text_lower, re.IGNORECASE):
                    matched_patterns.append({
                        'pattern_type': pattern_name,
                        'emotions': pattern_info['emotions'],
                        'urgency': pattern_info['urgency']
                    })
                    break
        
        return matched_patterns
    
    def _fuse_emotions(self, text_emotions: List, pattern_emotions: List, behavior_emotions: List) -> Dict:
        """
        融合多种情感识别结果
        
        使用加权融合策略：
        - 文本情感权重: 0.5
        - 模式情感权重: 0.3
        - 行为情感权重: 0.2
        
        Args:
            text_emotions: 文本情感识别结果
            pattern_emotions: 模式匹配结果
            behavior_emotions: 行为推断结果
            
        Returns:
            融合后的主导情感
        """
        emotion_scores = defaultdict(lambda: {'intensity': 0, 'confidence': 0, 'weight': 0})
        
        # 处理文本情感
        for i, e in enumerate(text_emotions):
            state = self.occ_model.map_to_emotional_state(e['emotion_key'])
            weight = 0.5 * e['confidence']
            emotion_scores[state]['intensity'] += e['intensity'] * weight
            emotion_scores[state]['confidence'] += e['confidence'] * weight
            emotion_scores[state]['weight'] += weight
        
        # 处理模式情感
        for pattern in pattern_emotions:
            for state in pattern['emotions']:
                weight = 0.3
                urgency_mult = {'high': 1.3, 'medium': 1.0, 'low': 0.7}
                intensity = 0.6 * urgency_mult.get(pattern['urgency'], 1.0)
                emotion_scores[state]['intensity'] += intensity * weight
                emotion_scores[state]['confidence'] += 0.7 * weight
                emotion_scores[state]['weight'] += weight
        
        # 处理行为情感
        for e in behavior_emotions:
            state = self.occ_model.map_to_emotional_state(e['emotion_key'])
            weight = 0.2 * e['confidence']
            emotion_scores[state]['intensity'] += e['intensity'] * weight
            emotion_scores[state]['confidence'] += e['confidence'] * weight
            emotion_scores[state]['weight'] += weight
        
        # 计算最终分数并选择主导情感
        if not emotion_scores:
            return {
                'state': EmotionalState.ENGAGED,
                'intensity': 0.5,
                'confidence': 0.3,
                'triggers': []
            }
        
        final_scores = {}
        for state, scores in emotion_scores.items():
            if scores['weight'] > 0:
                final_scores[state] = (scores['intensity'] / scores['weight']) * scores['confidence']
            else:
                final_scores[state] = 0
        
        dominant_state = max(final_scores, key=final_scores.get)
        dominant_intensity = emotion_scores[dominant_state]['intensity'] / max(emotion_scores[dominant_state]['weight'], 0.1)
        dominant_confidence = emotion_scores[dominant_state]['confidence'] / max(emotion_scores[dominant_state]['weight'], 0.1)
        
        return {
            'state': dominant_state,
            'intensity': min(dominant_intensity, 1.0),
            'confidence': min(dominant_confidence, 1.0),
            'triggers': []
        }
    
    def _get_occ_type(self, state: EmotionalState) -> EmotionType:
        """
        将EmotionalState映射到OCC EmotionType
        
        Args:
            state: 情感状态
            
        Returns:
            对应的OCC情感类型
        """
        mapping = {
            EmotionalState.EXCITED: EmotionType.JOY,
            EmotionalState.SATISFIED: EmotionType.SATISFACTION,
            EmotionalState.CONFIDENT: EmotionType.PRIDE,
            EmotionalState.MOTIVATED: EmotionType.HOPE,
            EmotionalState.CONFUSED: EmotionType.CONFUSION,
            EmotionalState.FRUSTRATED: EmotionType.FRUSTRATION,
            EmotionalState.ANXIOUS: EmotionType.ANXIETY,
            EmotionalState.BORED: EmotionType.BOREDOM,
            EmotionalState.ANGRY: EmotionType.ANGER,
            EmotionalState.CURIOUS: EmotionType.CURIOSITY,
            EmotionalState.ENGAGED: EmotionType.JOY,
        }
        return mapping.get(state, EmotionType.JOY)
    
    def _update_emotion_history(self, user_id: int, emotion: EmotionData, session_id: Optional[int] = None):
        """
        更新用户情感历史
        
        Args:
            user_id: 用户ID
            emotion: 情感数据
            session_id: 会话ID
        """
        if user_id not in self.emotion_histories:
            self.emotion_histories[user_id] = EmotionHistory(
                user_id=user_id,
                session_id=session_id
            )
        
        history = self.emotion_histories[user_id]
        
        # 检测情感转变
        if history.emotions:
            history.detect_emotional_shift(history.emotions[-1], emotion)
        
        # 添加情感记录
        history.add_emotion(emotion)
    
    def get_emotion_trend(self, user_id: int, time_window: int = 10) -> Dict:
        """
        获取用户情感趋势
        
        Args:
            user_id: 用户ID
            time_window: 分析的时间窗口（最近的N条记录）
            
        Returns:
            情感趋势分析结果
        """
        if user_id not in self.emotion_histories:
            return {
                'trend': 'neutral',
                'dominant_emotion': None,
                'emotional_valence': 0.0,
                'arousal_level': 0.0,
                'emotion_sequence': []
            }
        
        history = self.emotion_histories[user_id]
        recent_emotions = history.emotions[-time_window:] if history.emotions else []
        
        # 计算情感序列
        emotion_sequence = [e.state.value for e in recent_emotions]
        
        # 判断趋势
        if len(recent_emotions) >= 2:
            recent_valence = sum(e.intensity for e in recent_emotions[-3:]) / 3
            older_valence = sum(e.intensity for e in recent_emotions[:3]) / 3
            
            if recent_valence > older_valence + 0.2:
                trend = 'improving'
            elif recent_valence < older_valence - 0.2:
                trend = 'declining'
            else:
                trend = 'stable'
        else:
            trend = 'insufficient_data'
        
        return {
            'trend': trend,
            'dominant_emotion': history.dominant_emotion.value if history.dominant_emotion else None,
            'emotional_valence': history.emotional_valence,
            'arousal_level': history.arousal_level,
            'emotion_sequence': emotion_sequence,
            'total_emotions': len(history.emotions),
            'emotional_shifts': len(history.emotional_shifts)
        }
    
    def predict_emotional_state(self, user_id: int, next_action: str) -> EmotionalState:
        """
        预测用户的下一个情感状态
        
        基于当前情感历史和预期行动预测
        
        Args:
            user_id: 用户ID
            next_action: 预期的下一个行动
            
        Returns:
            预测的情感状态
        """
        trend = self.get_emotion_trend(user_id)
        current_emotion = trend.get('dominant_emotion')
        
        # 基于当前状态和预期行动预测
        if current_emotion in [EmotionalState.CONFUSED, EmotionalState.FRUSTRATED]:
            if 'hint' in next_action.lower() or 'help' in next_action.lower():
                return EmotionalState.CURIOUS
            else:
                return EmotionalState.FRUSTRATED
        
        elif current_emotion == EmotionalState.CONFIDENT:
            if 'difficult' in next_action.lower():
                return EmotionalState.CURIOUS
            else:
                return EmotionalState.CONFIDENT
        
        elif current_emotion == EmotionalState.BORED:
            return EmotionalState.MOTIVATED if 'new' in next_action.lower() else EmotionalState.BORED
        
        return EmotionalState.ENGAGED
    
    def analyze_emotional_context(self, user_id: int, context: Dict) -> Dict:
        """
        分析情感上下文
        
        整合情感历史、当前情感和情境信息
        
        Args:
            user_id: 用户ID
            context: 情境信息
            
        Returns:
            情感上下文分析结果
        """
        trend = self.get_emotion_trend(user_id)
        
        # 当前学习状态评估
        learning_struggle = trend.get('emotional_valence', 0) < -0.3
        high_engagement = trend.get('emotional_valence', 0) > 0.5
        
        return {
            'current_emotion': trend.get('dominant_emotion'),
            'emotional_trend': trend.get('trend'),
            'valence': trend.get('emotional_valence', 0),
            'arousal': trend.get('arousal_level', 0),
            'is_struggling': learning_struggle,
            'is_highly_engaged': high_engagement,
            'needs_encouragement': trend.get('emotional_valence', 0) < 0,
            'needs_challenge': trend.get('emotional_valence', 0) > 0.6 and trend.get('arousal_level', 0) < 0.4,
            'emotion_sequence': trend.get('emotion_sequence', [])
        }


class EmpathyResponseGenerator:
    """
    同理心响应生成器 - 论文核心实现
    
    基于检测到的情感生成同理心响应
    
    功能：
    1. 并行同理心响应 (Parallel Empathy)
    2. 反应性同理心响应 (Reactive Empathy)
    3. 认知同理心响应 (Cognitive Empathy)
    4. 行为同理心响应 (Behavioral Empathy)
    """
    
    # 同理心响应模板
    EMPATHY_TEMPLATES = {
        EmotionalState.CONFUSED: {
            EmpathyStrategy.PARALLEL_EMPATHY: [
                "我能感受到你现在有些困惑，这很正常。",
                "理解你的困惑，让我们一起来理清思路。",
            ],
            EmpathyStrategy.REACTIVE_EMPATHY: [
                "我看到你在思考这个问题，确实需要仔细分析。",
                "这个问题确实有点复杂，让我帮你梳理一下。",
            ],
            EmpathyStrategy.COGNITIVE_EMPATHY: [
                "你在努力理解这个概念，这说明你在积极思考。",
                "困惑往往意味着你正在接近理解的关键点。",
            ],
            EmpathyStrategy.BEHAVIORAL_EMPATHY: [
                "让我们把这个问题分解成更小的部分来理解。",
                "我先给你一个更简单的例子，然后我们再深入。",
            ]
        },
        EmotionalState.FRUSTRATED: {
            EmpathyStrategy.PARALLEL_EMPATHY: [
                "我能感受到你有些沮丧，没关系，我们一起慢慢来。",
                "理解你的挫败感，这种感觉在学习的道路上很正常。",
            ],
            EmpathyStrategy.REACTIVE_EMPATHY: [
                "我看到你遇到了困难，这确实不容易。",
                "面对这样的挑战感到沮丧是完全正常的反应。",
            ],
            EmpathyStrategy.COGNITIVE_EMPATHY: [
                "你已经尝试了很多次，这说明你很有毅力。",
                "每一次尝试都是学习的一部分，即使是挫折也是进步。",
            ],
            EmpathyStrategy.BEHAVIORAL_EMPATHY: [
                "让我们换个角度来看这个问题，也许会有新的发现。",
                "我先用更简单的方式解释，然后我们一步步来。",
            ]
        },
        EmotionalState.EXCITED: {
            EmpathyStrategy.PARALLEL_EMPATHY: [
                "我感受到你的兴奋！继续保持这份热情！",
                "太棒了！你的积极态度会让学习更加高效。",
            ],
            EmpathyStrategy.REACTIVE_EMPATHY: [
                "看到你这么有热情，我也被感染了！",
                "你的好奇心正是学习最好的动力！",
            ],
            EmpathyStrategy.COGNITIVE_EMPATHY: [
                "这份热情会帮助你克服接下来可能遇到的困难。",
                "保持这种探索精神，你会学到更多！",
            ],
            EmpathyStrategy.BEHAVIORAL_EMPATHY: [
                "既然你这么有热情，我们来挑战一个更有难度的问题吧！",
                "让我们利用这份能量，深入探索这个主题！",
            ]
        },
        EmotionalState.ANXIOUS: {
            EmpathyStrategy.PARALLEL_EMPATHY: [
                "我能理解你的紧张，深呼吸，我们一步一步来。",
                "感受到你的担忧，这是很正常的反应。",
            ],
            EmpathyStrategy.REACTIVE_EMPATHY: [
                "别太担心，我会一直在这里帮助你。",
                "让我们把大目标分解成小步骤，一步步完成。",
            ],
            EmpathyStrategy.COGNITIVE_EMPATHY: [
                "你已经做了很多准备，相信自己的能力。",
                "适度的紧张实际上可以帮助你保持专注。",
            ],
            EmpathyStrategy.BEHAVIORAL_EMPATHY: [
                "先从一个简单的问题开始，建立信心。",
                "让我先帮你梳理一下你已经掌握的知识点。",
            ]
        },
        EmotionalState.CONFIDENT: {
            EmpathyStrategy.PARALLEL_EMPATHY: [
                "看到你这么自信，我也很受鼓舞！",
                "你的信心建立在扎实的理解上，继续保持！",
            ],
            EmpathyStrategy.REACTIVE_EMPATHY: [
                "很好！你的努力正在看到成效。",
                "自信的态度会帮助你走得更远！",
            ],
            EmpathyStrategy.COGNITIVE_EMPATHY: [
                "你可以尝试一些更具挑战性的内容来拓展自己。",
                "你的进步证明了你完全有能力掌握这些知识。",
            ],
            EmpathyStrategy.BEHAVIORAL_EMPATHY: [
                "既然你这么有信心，我们来测试一下你的理解！",
                "要不要尝试一个更有难度的题目？",
            ]
        },
        EmotionalState.BORED: {
            EmpathyStrategy.PARALLEL_EMPATHY: [
                "我能理解这个内容可能让你觉得有些枯燥。",
                "学习有时候确实会让人感到单调，让我们来点不一样的。",
            ],
            EmpathyStrategy.REACTIVE_EMPATHY: [
                "也许我们可以换一种方式来学习这个内容？",
                "如果你觉得无聊，我们来加点趣味性。",
            ],
            EmpathyStrategy.COGNITIVE_EMPATHY: [
                "其实这个知识点在实际生活中有很多有趣的应用。",
                "理解了这个内容的核心，你会发现它其实很有价值。",
            ],
            EmpathyStrategy.BEHAVIORAL_EMPATHY: [
                "让我们用一些实际的例子来让这个内容更加生动。",
                "我们来做一个有趣的练习，让学习变得更有意思！",
            ]
        }
    }
    
    def __init__(self, llm_client=None):
        """
        初始化同理心响应生成器
        
        Args:
            llm_client: 大模型客户端，用于生成个性化响应
        """
        self.llm_client = llm_client
    
    def generate_empathetic_response(
        self,
        emotion: EmotionData,
        strategy: EmpathyStrategy = EmpathyStrategy.REACTIVE_EMPATHY,
        base_response: Optional[str] = None
    ) -> str:
        """
        生成同理心响应
        
        Args:
            emotion: 当前情感数据
            strategy: 同理心策略
            base_response: 基础响应内容
            
        Returns:
            包含同理心的响应
        """
        state = emotion.state
        
        # 获取对应情感的响应模板
        if state in self.EMPATHY_TEMPLATES:
            templates = self.EMPATHY_TEMPLATES[state].get(strategy, [])
            if templates:
                # 根据情感强度选择模板
                if emotion.intensity > 0.7:
                    template = templates[0]  # 更强烈的同理心
                else:
                    template = templates[-1]  # 温和的同理心
                
                if base_response:
                    return f"{template} {base_response}"
                return template
        
        # 默认响应
        if base_response:
            return base_response
        return ""
    
    def select_optimal_strategy(self, emotion: EmotionData, context: Dict) -> EmpathyStrategy:
        """
        选择最优的同理心策略
        
        基于情感状态和上下文选择最佳策略
        
        Args:
            emotion: 当前情感数据
            context: 上下文信息
            
        Returns:
            最优的同理心策略
        """
        # 高强度负面情感：优先使用行为同理心
        if emotion.intensity > 0.7 and emotion.state in [
            EmotionalState.FRUSTRATED,
            EmotionalState.ANXIOUS,
            EmotionalState.OVERWHELMED
        ]:
            return EmpathyStrategy.BEHAVIORAL_EMPATHY
        
        # 中等强度情感：使用反应性同理心
        if emotion.intensity > 0.4:
            return EmpathyStrategy.REACTIVE_EMPATHY
        
        # 正面情感：使用并行同理心
        if emotion.state in [
            EmotionalState.EXCITED,
            EmotionalState.CONFIDENT,
            EmotionalState.SATISFIED
        ]:
            return EmpathyStrategy.PARALLEL_EMPATHY
        
        # 不确定/好奇：使用认知同理心
        if emotion.state in [
            EmotionalState.CURIOUS,
            EmotionalState.UNCERTAIN,
            EmotionalState.CONFUSED
        ]:
            return EmpathyStrategy.COGNITIVE_EMPATHY
        
        # 默认
        return EmpathyStrategy.REACTIVE_EMPATHY
    
    def generate_multimodal_response(
        self,
        emotion: EmotionData,
        response: str,
        include_gesture_suggestion: bool = True
    ) -> Dict:
        """
        生成多模态响应（包括手势建议）
        
        基于论文中提到的手势控制功能
        
        Args:
            emotion: 当前情感数据
            response: 文本响应
            include_gesture_suggestion: 是否包含手势建议
            
        Returns:
            多模态响应，包含文本、手势、表情等
        """
        multimodal_response = {
            'text': response,
            'emotion_context': emotion.to_dict()
        }
        
        if include_gesture_suggestion:
            # 根据情感选择手势
            gesture = self._select_gesture(emotion)
            multimodal_response['gesture'] = gesture
            
            # 根据情感选择表情
            expression = self._select_expression(emotion)
            multimodal_response['expression'] = expression
        
        return multimodal_response
    
    def _select_gesture(self, emotion: EmotionData) -> Dict:
        """
        选择对应情感的手势
        
        基于论文中的手势控制研究
        
        Args:
            emotion: 当前情感数据
            
        Returns:
            手势建议
        """
        gestures = {
            EmotionalState.CONFUSED: {
                'type': 'pointing',
                'description': '指向思考/分析的动作',
                'action': '用手指向大脑或书本来表示思考'
            },
            EmotionalState.FRUSTRATED: {
                'type': 'calming',
                'description': '安抚性的手势',
                'action': '手掌向下轻轻摆动，表示"没关系"'
            },
            EmotionalState.EXCITED: {
                'type': 'encouraging',
                'description': '鼓励性的手势',
                'action': '竖起大拇指或鼓掌'
            },
            EmotionalState.ANXIOUS: {
                'type': 'grounding',
                'description': '接地性手势',
                'action': '双手放在膝盖上，深呼吸'
            },
            EmotionalState.CURIOUS: {
                'type': 'exploring',
                'description': '探索性手势',
                'action': '手指指向+轻微的头部倾斜'
            },
            EmotionalState.CONFIDENT: {
                'type': 'affirmative',
                'description': '肯定性手势',
                'action': '点头+手势确认'
            }
        }
        
        return gestures.get(emotion.state, {
            'type': 'neutral',
            'description': '中性手势',
            'action': '自然站立，保持开放姿态'
        })
    
    def _select_expression(self, emotion: EmotionData) -> Dict:
        """
        选择对应情感的表情
        
        Args:
            emotion: 当前情感数据
            
        Returns:
            表情建议
        """
        expressions = {
            EmotionalState.CONFUSED: {
                'primary': 'concerned',
                'description': '关切的表情',
                'eyes': '微微皱眉，目光专注'
            },
            EmotionalState.FRUSTRATED: {
                'primary': 'understanding',
                'description': '理解的表情',
                'eyes': '柔和的目光，表示同情'
            },
            EmotionalState.EXCITED: {
                'primary': 'happy',
                'description': '开心的表情',
                'eyes': '眼睛明亮带笑意'
            },
            EmotionalState.ANXIOUS: {
                'primary': 'reassuring',
                'description': '安抚的表情',
                'eyes': '温和坚定的目光'
            },
            EmotionalState.CURIOUS: {
                'primary': 'interested',
                'description': '好奇的表情',
                'eyes': '眼睛微微睁大'
            },
            EmotionalState.CONFIDENT: {
                'primary': 'assured',
                'description': '自信的表情',
                'eyes': '坚定的目光'
            }
        }
        
        return expressions.get(emotion.state, {
            'primary': 'neutral',
            'description': '中性表情',
            'eyes': '自然放松的目光'
        })


# 全局情感识别引擎实例
_emotion_engine: Optional[EmotionRecognitionEngine] = None
_empathy_generator: Optional[EmpathyResponseGenerator] = None


def get_emotion_engine(llm_client=None) -> EmotionRecognitionEngine:
    """
    获取全局情感识别引擎实例
    
    Args:
        llm_client: 大模型客户端
        
    Returns:
        EmotionRecognitionEngine实例
    """
    global _emotion_engine
    if _emotion_engine is None:
        _emotion_engine = EmotionRecognitionEngine(llm_client)
    return _emotion_engine


def get_empathy_generator(llm_client=None) -> EmpathyResponseGenerator:
    """
    获取全局同理心响应生成器实例
    
    Args:
        llm_client: 大模型客户端
        
    Returns:
        EmpathyResponseGenerator实例
    """
    global _empathy_generator
    if _empathy_generator is None:
        _empathy_generator = EmpathyResponseGenerator(llm_client)
    return _empathy_generator


def process_emotional_interaction(
    user_id: int,
    text: str,
    context: Optional[Dict] = None,
    llm_client=None
) -> Dict:
    """
    处理情感交互的主函数
    
    整合情感识别和同理心响应生成
    
    Args:
        user_id: 用户ID
        text: 用户输入文本
        context: 上下文信息
        llm_client: 大模型客户端
        
    Returns:
        包含情感分析和同理心响应的字典
    """
    # 获取引擎实例
    engine = get_emotion_engine(llm_client)
    generator = get_empathy_generator(llm_client)
    
    # 1. 识别情感
    emotion = engine.recognize_emotion(user_id, text, context)
    
    # 2. 分析情感上下文
    emotion_context = engine.analyze_emotional_context(user_id, context or {})
    
    # 3. 获取情感趋势
    trend = engine.get_emotion_trend(user_id)
    
    # 4. 选择最优同理心策略
    strategy = generator.select_optimal_strategy(emotion, context or {})
    
    # 5. 生成同理心响应（不含基础响应，仅同理心部分）
    empathy_response = generator.generate_empathetic_response(emotion, strategy)
    
    # 6. 生成多模态响应
    multimodal = generator.generate_multimodal_response(emotion, empathy_response)
    
    return {
        'emotion': emotion.to_dict(),
        'emotion_context': emotion_context,
        'emotion_trend': trend,
        'empathy_strategy': strategy.value,
        'empathy_response': empathy_response,
        'multimodal_response': multimodal
    }
