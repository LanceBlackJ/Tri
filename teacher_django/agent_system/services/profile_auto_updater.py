"""
画像自动更新引擎 - 核心服务
根据用户行为自动更新画像的各个维度
"""
import copy
import logging
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from django.contrib.auth import get_user_model
from django.utils import timezone

from ..models import StudentProfile

logger = logging.getLogger(__name__)
User = get_user_model()


class ConfidenceTracker:
    """
    置信度追踪器
    用于追踪每个画像维度的置信度
    """
    
    # 置信度等级
    LEVEL_LOW = 0.3
    LEVEL_MEDIUM = 0.6
    LEVEL_HIGH = 0.8
    
    @classmethod
    def adjust_confidence(
        cls,
        current_confidence: float,
        signal_strength: float,
        data_age_days: int = 0,
    ) -> float:
        """
        根据信号强度和数据时效调整置信度
        
        Args:
            current_confidence: 当前置信度
            signal_strength: 信号强度 (0.0 ~ 1.0)
            data_age_days: 数据年龄（天）
            
        Returns:
            调整后的置信度
        """
        # 时间衰减因子：每30天衰减10%
        age_factor = max(0.5, 1.0 - (data_age_days / 30) * 0.1)
        
        # 旧数据难以提高置信度
        if current_confidence > 0.5 and signal_strength < 0.3:
            return current_confidence * 0.95
        
        # 新数据根据信号强度增加置信度
        if signal_strength >= 0.5:
            new_confidence = current_confidence + (signal_strength * 0.2 * age_factor)
        else:
            new_confidence = current_confidence + (signal_strength * 0.1 * age_factor)
        
        return max(0.0, min(1.0, new_confidence))


class KnowledgeUpdater:
    """
    知识掌握度更新器
    
    规则:
    - 答对: +0.1 ~ +0.25 (取决于题目难度)
    - 答错: -0.15 ~ -0.30
    - 连续答对: 奖励加成
    - 连续答错: 惩罚加成
    - 掌握度范围: 0.0 ~ 1.0
    """
    
    DIFFICULTY_WEIGHTS = {
        'easy': {'correct': 0.1, 'wrong': -0.15},
        'standard': {'correct': 0.15, 'wrong': -0.2},
        'hard': {'correct': 0.2, 'wrong': -0.25},
        'challenge': {'correct': 0.25, 'wrong': -0.3},
    }
    
    STREAK_BONUS = 0.05
    STREAK_THRESHOLD = 3
    MIN_VALUE = 0.0
    MAX_VALUE = 1.0

    LEVEL_LABEL_SCORES = {'高级': 0.95, '中级': 0.65, '初级': 0.3}

    @classmethod
    def normalize_level(cls, value: Any) -> float:
        """将 knowledge_profile 中各种历史格式统一转换为0.0~1.0的掌握度。

        knowledge_profile[tag] 在不同写入路径下可能是：
        - 0~1 的浮点数（本更新器写入的格式）
        - dict，如 {'mastery_score': 85, ...}（材料测验写入，0-100量表）
        - 字符串等级 '初级'/'中级'/'高级'（对话画像构建写入）
        """
        if isinstance(value, dict):
            value = value.get('mastery_score')
        if isinstance(value, bool):
            return 0.5
        if isinstance(value, (int, float)):
            level = float(value)
            if level > cls.MAX_VALUE:
                level /= 100.0
            return max(cls.MIN_VALUE, min(cls.MAX_VALUE, level))
        if isinstance(value, str):
            return cls.LEVEL_LABEL_SCORES.get(value, 0.5)
        return 0.5

    @classmethod
    def calculate_new_level(
        cls,
        current_level: float,
        is_correct: bool,
        difficulty: str = 'standard',
        consecutive_count: int = 0,
    ) -> Tuple[float, float]:
        """
        计算新的知识掌握度
        
        Returns:
            (new_level, confidence_delta)
        """
        weights = cls.DIFFICULTY_WEIGHTS.get(difficulty, cls.DIFFICULTY_WEIGHTS['standard'])
        
        if is_correct:
            delta = weights['correct']
            # 连续正确加成
            if consecutive_count >= cls.STREAK_THRESHOLD:
                delta += cls.STREAK_BONUS
        else:
            delta = weights['wrong']
            # 连续错误惩罚
            if consecutive_count >= cls.STREAK_THRESHOLD:
                delta -= cls.STREAK_BONUS
        
        new_level = max(cls.MIN_VALUE, min(cls.MAX_VALUE, current_level + delta))
        confidence_delta = abs(delta) * 0.5

        return new_level, confidence_delta


class BKTUpdater:
    """
    贝叶斯知识追踪更新器（Corbett & Anderson, 1994）+ 遗忘衰减扩展（FoLiBiKT 风格）

    P(T): 学习迁移概率；P(S): 失误概率（已掌握但答错）；P(G): 猜测概率（未掌握但答对）。
    遗忘衰减：掌握度按经过天数向 FORGETTING_BASELINE 指数衰减，衰减在贝叶斯更新前应用。
    """

    BKT_PARAMS = {
        'standard':    {'p_transit': 0.10, 'p_guess': 0.20, 'p_slip': 0.10},
        'reinforce':   {'p_transit': 0.08, 'p_guess': 0.25, 'p_slip': 0.12},
        'progressive': {'p_transit': 0.12, 'p_guess': 0.15, 'p_slip': 0.08},
        'challenge':   {'p_transit': 0.15, 'p_guess': 0.10, 'p_slip': 0.05},
    }

    FORGETTING_BASELINE = 0.4
    FORGETTING_DECAY_RATE = 0.04  # 每天

    RETENTION_THRESHOLD = 0.6  # 默认"建议复习"的掌握度阈值

    MIN_VALUE = 0.0
    MAX_VALUE = 1.0

    @classmethod
    def apply_forgetting_decay(cls, level: float, days_elapsed: float) -> float:
        if days_elapsed <= 0:
            return level
        # 遗忘只会让掌握度向基线衰减、不会提升。掌握度本就低于（或等于）基线的知识点，
        # 不学习不应“自动变好”——直接原样返回，避免弱知识点被人为抬高到基线附近。
        if level <= cls.FORGETTING_BASELINE:
            return level
        decay = math.exp(-cls.FORGETTING_DECAY_RATE * days_elapsed)
        return cls.FORGETTING_BASELINE + (level - cls.FORGETTING_BASELINE) * decay

    @staticmethod
    def days_elapsed_since(timestamp_str: Optional[str], now: Optional[datetime] = None) -> float:
        """计算距 ISO8601 时间戳已过去的天数；无/非法时间戳返回0。"""
        if not timestamp_str:
            return 0.0
        now = now or timezone.now()
        try:
            ts = str(timestamp_str).strip()
            if ts.endswith('Z'):  # 兼容以 Z 结尾的 UTC 时间戳（fromisoformat 在旧版不接受 Z）
                ts = ts[:-1] + '+00:00'
            last_ts = datetime.fromisoformat(ts)
            if last_ts.tzinfo is None:
                last_ts = timezone.make_aware(last_ts, timezone.get_default_timezone())
            return max(0.0, (now - last_ts).total_seconds() / 86400.0)
        except (ValueError, TypeError):
            return 0.0

    @classmethod
    def predict_days_to_threshold(cls, level: float, threshold: Optional[float] = None) -> float:
        """
        预测掌握度因遗忘衰减降至 threshold 所需天数（apply_forgetting_decay 的反函数）。

        - level <= threshold: 已处于阈值以下，返回 0（立即需要复习）。
        - threshold <= FORGETTING_BASELINE < level: 衰减只会把 level 拉向 baseline，
          永远不会跌破一个 <= baseline 的阈值，返回 inf。
        - 否则: 解 threshold = baseline + (level-baseline)*exp(-rate*d) 得到 d。
        """
        threshold = cls.RETENTION_THRESHOLD if threshold is None else threshold
        if level <= threshold:
            return 0.0
        if threshold <= cls.FORGETTING_BASELINE:
            return float('inf')
        ratio = (threshold - cls.FORGETTING_BASELINE) / (level - cls.FORGETTING_BASELINE)
        return -math.log(ratio) / cls.FORGETTING_DECAY_RATE

    @classmethod
    def calculate_new_level(
        cls,
        current_level: float,
        is_correct: bool,
        difficulty: str = 'standard',
        days_elapsed: float = 0.0,
    ) -> Tuple[float, float]:
        """
        计算新的知识掌握度（贝叶斯后验 + 学习迁移）

        Returns:
            (new_level, confidence_delta)
        """
        params = cls.BKT_PARAMS.get(difficulty, cls.BKT_PARAMS['standard'])
        pt, pg, ps = params['p_transit'], params['p_guess'], params['p_slip']

        level = cls.apply_forgetting_decay(current_level, days_elapsed)

        if is_correct:
            numerator = level * (1 - ps)
            denominator = numerator + (1 - level) * pg
        else:
            numerator = level * ps
            denominator = numerator + (1 - level) * (1 - pg)

        posterior = numerator / denominator if denominator > 0 else level
        new_level = posterior + (1 - posterior) * pt
        new_level = max(cls.MIN_VALUE, min(cls.MAX_VALUE, new_level))

        confidence_delta = abs(new_level - current_level) * 0.5
        return new_level, confidence_delta


def build_review_queue(knowledge_profile, knowledge_timestamps, threshold=None, limit=5, now=None):
    """
    构建"今日复习推荐"队列：按遗忘衰减预测各知识点跌破 threshold 的剩余天数排序
    （最紧迫/已逾期的排最前）。

    返回: [{tag, mastery, current_retention, days_elapsed, days_until_due, is_due}, ...]
    跌破阈值天数为 inf 的知识点（衰减不会使其低于阈值）不出现在队列中。
    """
    threshold = BKTUpdater.RETENTION_THRESHOLD if threshold is None else threshold
    now = now or timezone.now()
    items = []
    for tag, raw_level in (knowledge_profile or {}).items():
        # __ 前缀是内部元数据；'overall' 是文本推断写入的整体水平摘要，不是具体知识点，都不进复习队列
        if tag.startswith('__') or tag == 'overall':
            continue
        level = KnowledgeUpdater.normalize_level(raw_level)
        days_elapsed = BKTUpdater.days_elapsed_since((knowledge_timestamps or {}).get(tag), now)

        days_to_threshold = BKTUpdater.predict_days_to_threshold(level, threshold)
        if math.isinf(days_to_threshold):
            continue

        days_until_due = days_to_threshold - days_elapsed
        items.append({
            'tag': tag,
            'mastery': round(level, 3),
            'current_retention': round(BKTUpdater.apply_forgetting_decay(level, days_elapsed), 3),
            'days_elapsed': round(days_elapsed, 1),
            'days_until_due': round(days_until_due, 1),
            'is_due': days_until_due <= 0,
        })

    items.sort(key=lambda item: item['days_until_due'])
    return items[:limit]


class MisconceptionUpdater:
    """
    易错点更新器
    
    触发条件:
    - 同一知识点连续答错 2 次 → 记录为易错点
    - 同一知识点累计答错 3 次 → 标记为顽固易错点
    """
    
    CONSECUTIVE_WRONG_THRESHOLD = 2
    TOTAL_WRONG_THRESHOLD = 3
    
    @classmethod
    def build_misconception_entry(
        cls,
        knowledge_tag: str,
        wrong_details: str = '',
        source_attempt_id: int = 0,
    ) -> dict:
        """构建易错点条目"""
        return {
            'tag': knowledge_tag,
            'wrong_count': 1,
            'consecutive_wrong': 1,
            'first_wrong_at': datetime.now().isoformat(),
            'last_wrong_at': datetime.now().isoformat(),
            'wrong_details': wrong_details,
            'source_attempt_id': source_attempt_id,
            'is_persistent': False,
            'status': 'active',
            'resolved_count': 0,
        }

    @classmethod
    def coerce_entry(cls, value: Any) -> dict:
        """将历史写入路径（如 profile_events._build_quiz_delta）产生的纯字符串
        易错点条目统一转换为本类使用的 dict 结构，避免 .get('tag') 报错。"""
        if isinstance(value, dict):
            return value
        return cls.build_misconception_entry(knowledge_tag=str(value))

    @classmethod
    def update_misconception_entry(
        cls,
        entry: dict,
        is_correct_next: bool,
    ) -> dict:
        """更新已有易错点条目"""
        now = datetime.now().isoformat()
        
        if is_correct_next:
            entry['consecutive_wrong'] = 0
            entry['resolved_count'] = entry.get('resolved_count', 0) + 1
            if entry['resolved_count'] >= 3:
                entry['status'] = 'resolved'
        else:
            entry['wrong_count'] = entry.get('wrong_count', 0) + 1
            entry['consecutive_wrong'] = entry.get('consecutive_wrong', 0) + 1
            entry['last_wrong_at'] = now
            entry['resolved_count'] = 0  # 答错打断连续答对进度，复发的易错点重新累计
            entry['is_persistent'] = entry['wrong_count'] >= cls.TOTAL_WRONG_THRESHOLD
            entry['status'] = 'active'

        return entry
    
    @classmethod
    def should_add_misconception(
        cls,
        misconceptions: List[dict],
        knowledge_tag: str,
        consecutive_wrong: int,
    ) -> bool:
        """判断是否应该添加新的易错点"""
        if consecutive_wrong < cls.CONSECUTIVE_WRONG_THRESHOLD:
            return False
        
        # 检查是否已存在
        for m in misconceptions:
            if m.get('tag') == knowledge_tag and m.get('status') == 'active':
                return False
        
        return True


class CognitiveStyleInferrer:
    """
    认知风格推断器
    
    风格类型:
    - visual: 视觉型 - 倾向图示、图表
    - auditory: 听觉型 - 倾向讲解、讨论
    - kinesthetic: 动手型 - 倾向练习、实践
    - analytical: 分析型 - 倾向原理、证明
    - holistic: 整体型 - 倾向框架、概览
    - mixed: 混合型
    """
    
    STYLE_KEYWORDS = {
        'visual': ['图', '图示', '图表', '可视化', '颜色', '布局', '视觉', '画'],
        'auditory': ['听', '说', '讨论', '讲解', '口述', '音频', '语音'],
        'kinesthetic': ['练习', '实践', '动手', '操作', '做', '实验', '操作'],
        'analytical': ['原理', '证明', '推导', '逻辑', '分析', '为什么', '本质'],
        'holistic': ['框架', '概览', '整体', '全局', '大纲', '结构', '总结'],
    }
    
    STYLE_CONFIRMATION_THRESHOLD = 5  # 确认风格所需次数
    MIN_CONFIDENCE = 0.4
    
    @classmethod
    def infer_style_from_text(cls, text: str) -> Optional[str]:
        """从文本中推断认知风格"""
        if not text:
            return None
        
        style_counts = {style: 0 for style in cls.STYLE_KEYWORDS}
        
        for style, keywords in cls.STYLE_KEYWORDS.items():
            for keyword in keywords:
                if keyword in text:
                    style_counts[style] += 1
        
        # 找出最匹配的风格
        max_count = max(style_counts.values())
        if max_count == 0:
            return None
        
        inferred_styles = [s for s, c in style_counts.items() if c == max_count]
        return inferred_styles[0] if inferred_styles else None
    
    @classmethod
    def update_style_scores(
        cls,
        current_scores: dict,
        inferred_style: str,
    ) -> dict:
        """更新风格得分"""
        if not current_scores:
            current_scores = {style: 0 for style in cls.STYLE_KEYWORDS}
        
        # 增加推断风格的分值
        if inferred_style in current_scores:
            current_scores[inferred_style] += 1
        
        # 计算置信度
        total_signals = sum(current_scores.values())
        if total_signals == 0:
            return current_scores
        
        scores_with_confidence = {}
        for style, count in current_scores.items():
            confidence = count / total_signals if total_signals > 0 else 0
            scores_with_confidence[style] = {
                'count': count,
                'confidence': confidence,
            }
        
        return scores_with_confidence
    
    @classmethod
    def determine_style(cls, style_scores: dict) -> Tuple[str, float]:
        """
        根据风格得分确定用户风格
        
        Returns:
            (style_name, confidence)
        """
        if not style_scores:
            return 'mixed', 0.0
        
        # 找出最高得分
        max_count = 0
        dominant_style = 'mixed'
        
        for style, data in style_scores.items():
            if isinstance(data, dict):
                count = data.get('count', 0)
            else:
                count = data
            if count > max_count:
                max_count = count
                dominant_style = style
        
        # 计算置信度
        total = sum(
            d.get('count', 0) if isinstance(d, dict) else d
            for d in style_scores.values()
        )
        confidence = max_count / total if total > 0 else 0.0
        
        if confidence < cls.MIN_CONFIDENCE:
            return 'mixed', confidence
        
        return dominant_style, confidence


class EngagementCalculator:
    """
    参与度计算器
    
    指标:
    - frequency_score: 学习频率 (0-100)
    - consistency_score: 学习规律性 (0-100)
    - intensity_score: 学习强度 (0-100)
    - progress_score: 学习进度 (0-100)
    """
    
    # 评分区间
    EXCELLENT_THRESHOLD = 0.8
    GOOD_THRESHOLD = 0.5
    POOR_THRESHOLD = 0.3
    
    @classmethod
    def calculate_frequency_score(
        cls,
        sessions_last_7_days: int,
        sessions_last_30_days: int,
    ) -> Dict[str, Any]:
        """计算学习频率得分"""
        # 期望每天至少1次学习
        expected_weekly = 7
        expected_monthly = 25
        
        weekly_ratio = min(1.0, sessions_last_7_days / expected_weekly) if expected_weekly > 0 else 0
        monthly_ratio = min(1.0, sessions_last_30_days / expected_monthly) if expected_monthly > 0 else 0
        
        score = int((weekly_ratio * 0.6 + monthly_ratio * 0.4) * 100)
        
        return {
            'score': score,
            'sessions_7d': sessions_last_7_days,
            'sessions_30d': sessions_last_30_days,
            'level': cls._score_to_level(score),
        }
    
    @classmethod
    def calculate_consistency_score(
        cls,
        daily_sessions: List[int],  # 过去7天每天的session数
    ) -> Dict[str, Any]:
        """计算学习规律性得分"""
        if not daily_sessions:
            return {'score': 0, 'level': 'poor'}
        
        # 计算标准差
        mean = sum(daily_sessions) / len(daily_sessions)
        if mean == 0:
            return {'score': 0, 'level': 'poor'}
        
        variance = sum((x - mean) ** 2 for x in daily_sessions) / len(daily_sessions)
        std_dev = math.sqrt(variance)
        
        # 标准差越小，规律性越强
        cv = std_dev / mean if mean > 0 else 0  # 变异系数
        score = int(max(0, (1 - cv) * 100))
        
        return {
            'score': score,
            'mean_daily': round(mean, 1),
            'cv': round(cv, 2),
            'level': cls._score_to_level(score),
        }
    
    @classmethod
    def calculate_intensity_score(
        cls,
        avg_time_per_session_minutes: float,
        avg_questions_per_session: int,
    ) -> Dict[str, Any]:
        """计算学习强度得分"""
        # 期望: 每次30分钟，10道题
        time_score = min(1.0, avg_time_per_session_minutes / 30) if avg_time_per_session_minutes > 0 else 0
        question_score = min(1.0, avg_questions_per_session / 10) if avg_questions_per_session > 0 else 0
        
        score = int((time_score * 0.4 + question_score * 0.6) * 100)
        
        return {
            'score': score,
            'avg_time_min': round(avg_time_per_session_minutes, 1),
            'avg_questions': round(avg_questions_per_session, 1),
            'level': cls._score_to_level(score),
        }
    
    @classmethod
    def calculate_overall_score(
        cls,
        frequency: Dict[str, Any],
        consistency: Dict[str, Any],
        intensity: Dict[str, Any],
    ) -> Dict[str, Any]:
        """计算综合参与度得分"""
        f_score = frequency.get('score', 0)
        c_score = consistency.get('score', 0)
        i_score = intensity.get('score', 0)
        
        # 加权平均
        overall = int(f_score * 0.35 + c_score * 0.25 + i_score * 0.40)
        
        # 计算趋势：用前三周的平均周活动量作为基线
        # （此前 prev_week 误用了同一个 sessions_7d 值 → trend 恒为 stable）
        recent_week = frequency.get('sessions_7d', 0)
        sessions_30d = frequency.get('sessions_30d', 0)
        prev_weeks_avg = max(0.0, (sessions_30d - recent_week) / 3.0)

        if prev_weeks_avg <= 0:
            trend = 'improving' if recent_week > 0 else 'stable'
        elif recent_week > prev_weeks_avg * 1.2:
            trend = 'improving'
        elif recent_week < prev_weeks_avg * 0.8:
            trend = 'declining'
        else:
            trend = 'stable'
        
        return {
            'score': overall,
            'frequency': frequency,
            'consistency': consistency,
            'intensity': intensity,
            'trend': trend,
            'level': cls._score_to_level(overall),
        }
    
    @classmethod
    def _score_to_level(cls, score: int) -> str:
        """得分转等级"""
        if score >= 80:
            return 'excellent'
        elif score >= 60:
            return 'good'
        elif score >= 40:
            return 'fair'
        elif score >= 20:
            return 'poor'
        return 'very_poor'


class PreferenceDetector:
    """
    偏好检测器
    
    偏好维度:
    - content_format: 内容格式偏好 (文字/图表/视频/代码)
    - difficulty_preference: 难度偏好 (基础/进阶/挑战)
    - session_length: 偏好单次学习时长
    - review_style: 复习方式 (做题/重读/总结)
    - interaction_mode: 交互模式 (主动提问/被动接受)
    """
    
    FORMAT_KEYWORDS = {
        'text': ['文字', '文档', '阅读', '说明'],
        'diagram': ['图', '图表', '图示', '可视化'],
        'video': ['视频', '动画', '演示'],
        'code': ['代码', '编程', '实现', '示例'],
        'exercise': ['练习', '做题', '测试'],
    }
    
    @classmethod
    def infer_format_preference(cls, user_text: str = '', action: str = '') -> Optional[str]:
        """从文本或行为推断内容格式偏好"""
        text = user_text + ' ' + action
        
        for format_type, keywords in cls.FORMAT_KEYWORDS.items():
            for keyword in keywords:
                if keyword in text:
                    return format_type
        
        return None
    
    @classmethod
    def update_preference_scores(
        cls,
        current_prefs: dict,
        preference_type: str,
        preference_value: str,
    ) -> dict:
        """更新偏好得分"""
        if not current_prefs:
            current_prefs = {}
        
        if preference_type not in current_prefs:
            current_prefs[preference_type] = {}
        
        scores = current_prefs[preference_type]
        if isinstance(scores, list):
            # 转换为dict格式
            scores = {v: 1 for v in scores}
            current_prefs[preference_type] = scores
        
        scores[preference_value] = scores.get(preference_value, 0) + 1
        
        return current_prefs


class ProfileAutoUpdater:
    """
    画像自动更新引擎
    
    使用方式:
        updater = ProfileAutoUpdater(user)
        delta = updater.update_knowledge_from_quiz(
            knowledge_tags=['函数', '极限'],
            is_correct=False,
            difficulty='standard',
        )
    """
    
    def __init__(self, user):
        self.user = user
        self.profile, self.created = self._get_or_create_profile()
        self._delta = {}  # 记录本次更新的变化
    
    def _get_or_create_profile(self) -> Tuple[StudentProfile, bool]:
        """获取或创建用户画像"""
        profile, created = StudentProfile.objects.get_or_create(user=self.user)
        return profile, created
    
    def _save_delta(self, dimension: str, old_value: Any, new_value: Any):
        """记录更新变化。同一维度多次写入（如一次做题里多个 tag 各自改 misconceptions）时，
        保留最早的 before 和最新的 after，避免后一次覆盖前一次导致中间变化量丢失。"""
        if dimension in self._delta:
            self._delta[dimension]['after'] = new_value
        else:
            self._delta[dimension] = {
                'before': old_value,
                'after': new_value,
            }
    
    def _finalize(self) -> dict:
        """保存更新并返回变化量"""
        self.profile.save()
        return self._delta
    
    # ===== 知识掌握度更新 =====
    
    def update_knowledge_from_quiz(
        self,
        knowledge_tags: List[str],
        is_correct: bool,
        difficulty: str = 'standard',
        consecutive_count: int = 0,
        attempt_id: int = 0,
    ) -> dict:
        """
        从做题结果更新知识掌握度
        
        Args:
            knowledge_tags: 知识点列表
            is_correct: 是否正确
            difficulty: 难度等级
            consecutive_count: 连续答题次数
            attempt_id: 尝试ID
            
        Returns:
            更新变化量
        """
        if not knowledge_tags:
            return {}

        knowledge_profile = self.profile.knowledge_profile or {}
        knowledge_timestamps = self.profile.knowledge_timestamps or {}
        old_profile = knowledge_profile.copy()
        old_timestamps = knowledge_timestamps.copy()
        now = timezone.now()
        total_conf_delta = 0.0

        for tag in knowledge_tags:
            if not tag:
                continue

            current_level = KnowledgeUpdater.normalize_level(knowledge_profile.get(tag, 0.5))
            days_elapsed = BKTUpdater.days_elapsed_since(knowledge_timestamps.get(tag), now)

            new_level, conf_delta = BKTUpdater.calculate_new_level(
                current_level=current_level,
                is_correct=is_correct,
                difficulty=difficulty,
                days_elapsed=days_elapsed,
            )
            total_conf_delta += conf_delta
            knowledge_profile[tag] = round(new_level, 3)
            knowledge_timestamps[tag] = now.isoformat()

            # 如果答错，检查并更新易错点
            if not is_correct:
                self._check_and_add_misconception(tag, consecutive_count, attempt_id)
            # 如果答对，重置易错点的连续错误计数
            else:
                self._reset_misconception_on_correct(tag)

        self.profile.knowledge_profile = knowledge_profile
        self.profile.knowledge_timestamps = knowledge_timestamps
        # 做题带来的画像置信度增量（此前 BKT 返回的 confidence_delta 被丢弃）
        if total_conf_delta:
            try:
                self.profile.inference_confidence = min(1.0, float(self.profile.inference_confidence or 0.0) + round(total_conf_delta, 4))
            except Exception:
                pass
        self._save_delta('knowledge_profile', old_profile, knowledge_profile)
        self._save_delta('knowledge_timestamps', old_timestamps, knowledge_timestamps)

        return self._finalize()
    
    def _check_and_add_misconception(
        self,
        knowledge_tag: str,
        consecutive_wrong: int,
        attempt_id: int,
    ):
        """按"连续答错达到阈值(CONSECUTIVE_WRONG_THRESHOLD=2)才正式记为易错点"的设计：
        首次答错只在条目上低调追踪(status='tracking')、不计入正式易错点，
        连续第二次答错才 status='active'。避免"一次答错就报警"。
        """
        threshold = MisconceptionUpdater.CONSECUTIVE_WRONG_THRESHOLD
        misconceptions = self.profile.misconceptions or []
        # deepcopy 原值：coerce/原地改 dict 会连带改到浅拷贝里的同一批对象，导致 delta 的 before==after
        old_misconceptions = copy.deepcopy(list(misconceptions))
        misconceptions = [MisconceptionUpdater.coerce_entry(m) for m in misconceptions]

        entry = None
        for m in misconceptions:
            if m.get('tag') == knowledge_tag:
                entry = m
                break
        if entry is None:
            entry = MisconceptionUpdater.build_misconception_entry(
                knowledge_tag=knowledge_tag, wrong_details='', source_attempt_id=attempt_id,
            )
            entry['consecutive_wrong'] = 1
            # 新条目首次答错只追踪，不直接 active（build 默认 active 会架空"连续2次才记"的设计）
            entry['status'] = 'tracking'
            misconceptions.append(entry)
        else:
            # 再次答错：累计错题数、连续错、并清掉之前答对攒的 resolved 进度（答错打断连续答对）
            entry['wrong_count'] = int(entry.get('wrong_count', 0) or 0) + 1
            entry['consecutive_wrong'] = int(entry.get('consecutive_wrong', 0) or 0) + 1
            entry['last_wrong_at'] = timezone.now().isoformat()
            entry['resolved_count'] = 0
            entry['is_persistent'] = entry['wrong_count'] >= MisconceptionUpdater.TOTAL_WRONG_THRESHOLD

        # 达到阈值才激活为正式易错点；否则仅追踪
        if entry['consecutive_wrong'] >= threshold:
            entry['status'] = 'active'
        elif entry.get('status') != 'active':
            entry['status'] = 'tracking'

        self.profile.misconceptions = misconceptions
        self._save_delta('misconceptions', old_misconceptions, misconceptions)

    def _reset_misconception_on_correct(self, knowledge_tag: str):
        """当用户答对时，重置该知识点易错点的连续错误计数（含追踪中的条目）"""
        misconceptions = self.profile.misconceptions or []
        if not misconceptions:
            return

        old_misconceptions = copy.deepcopy(list(misconceptions))
        misconceptions = [MisconceptionUpdater.coerce_entry(m) for m in misconceptions]
        changed = False
        for m in misconceptions:
            if m.get('tag') != knowledge_tag:
                continue
            m['consecutive_wrong'] = 0
            changed = True
            if m.get('status') == 'active':
                m['resolved_count'] = m.get('resolved_count', 0) + 1
                if m['resolved_count'] >= 3:  # 连续答对3次标记为已解决
                    m['status'] = 'resolved'
                    m['resolved_at'] = timezone.now().isoformat()
            elif m.get('status') == 'tracking':
                # 还没升级为正式易错点就答对了，直接消除追踪
                m['status'] = 'resolved'
            break
        # coerce 可能把历史里的 str 条目规整成了 dict，即使没匹配到 tag 也写回一次（但不记 delta）
        self.profile.misconceptions = misconceptions
        if changed:
            self._save_delta('misconceptions', old_misconceptions, misconceptions)
    
    # ===== 易错点更新 =====
    
    def add_misconception(
        self,
        knowledge_tag: str,
        wrong_details: str = '',
        source_attempt_id: int = 0,
    ) -> dict:
        """添加易错点"""
        misconceptions = self.profile.misconceptions or []
        old_misconceptions = copy.deepcopy(list(misconceptions))
        misconceptions = [MisconceptionUpdater.coerce_entry(m) for m in misconceptions]

        # 检查是否已存在
        existing = None
        for m in misconceptions:
            if m.get('tag') == knowledge_tag:
                existing = m
                break
        
        if existing:
            # 更新现有易错点
            updated = MisconceptionUpdater.update_misconception_entry(
                existing, is_correct_next=False
            )
            # 替换
            for i, m in enumerate(misconceptions):
                if m.get('tag') == knowledge_tag:
                    misconceptions[i] = updated
                    break
        else:
            # 创建新条目
            entry = MisconceptionUpdater.build_misconception_entry(
                knowledge_tag=knowledge_tag,
                wrong_details=wrong_details,
                source_attempt_id=source_attempt_id,
            )
            misconceptions.append(entry)
        
        self.profile.misconceptions = misconceptions
        self._save_delta('misconceptions', old_misconceptions, misconceptions)
        
        return self._finalize()
    
    def resolve_misconception(self, knowledge_tag: str) -> dict:
        """标记易错点为已解决"""
        misconceptions = self.profile.misconceptions or []
        old_misconceptions = copy.deepcopy(list(misconceptions))
        misconceptions = [MisconceptionUpdater.coerce_entry(m) for m in misconceptions]

        for m in misconceptions:
            if m.get('tag') == knowledge_tag:
                m['status'] = 'resolved'
                m['resolved_at'] = datetime.now().isoformat()
        
        self.profile.misconceptions = misconceptions
        self._save_delta('misconceptions', old_misconceptions, misconceptions)
        
        return self._finalize()
    
    # ===== 认知风格更新 =====
    
    def update_cognitive_style_from_chat(
        self,
        question_type: str = '',
        user_message: str = '',
    ) -> dict:
        """从对话更新认知风格"""
        old_style = self.profile.cognitive_style
        
        # 推断风格
        inferred = CognitiveStyleInferrer.infer_style_from_text(
            question_type + ' ' + user_message
        )
        
        if inferred:
            self.profile.cognitive_style = inferred
            self._save_delta('cognitive_style', old_style, inferred)
        
        return self._finalize()
    
    # ===== 困惑信号 =====
    
    def add_confusion_signal(
        self,
        topic: str,
        question_text: str = '',
    ) -> dict:
        """记录困惑信号，可能识别为潜在易错点"""
        # 检查是否已有相关困惑记录
        misconceptions = self.profile.misconceptions or []
        old_misconceptions = copy.deepcopy(list(misconceptions))

        for m in misconceptions:
            if m.get('tag') == topic and m.get('status') == 'active':
                # 增加困惑计数
                m['confusion_count'] = m.get('confusion_count', 0) + 1
                m['last_confusion_at'] = timezone.now().isoformat()
                self.profile.misconceptions = misconceptions
                self._save_delta('misconceptions', old_misconceptions, misconceptions)
                return self._finalize()

        # 标记为潜在易错点
        entry = {
            'tag': topic,
            'type': 'potential_misconception',
            'confusion_count': 1,
            'first_confusion_at': timezone.now().isoformat(),
            'last_confusion_at': timezone.now().isoformat(),
            'question_sample': question_text[:200] if question_text else '',
            'status': 'potential',
        }
        misconceptions.append(entry)
        self.profile.misconceptions = misconceptions
        self._save_delta('misconceptions', old_misconceptions, misconceptions)

        return self._finalize()
    
    # ===== 参与度更新 =====
    
    def update_engagement(
        self,
        session_data: dict,
    ) -> dict:
        """更新参与度"""
        old_engagement = self.profile.engagement or {}
        
        engagement = old_engagement.copy()
        
        # 更新各项指标（统一用 timezone.now()，避免 naive/aware 混用）
        now = timezone.now()
        today = now.date().isoformat()

        # 学习频率
        sessions_today = dict(engagement.get('sessions_today', {}))
        sessions_today[today] = sessions_today.get(today, 0) + 1
        # 剪枝：只保留最近30天，避免 sessions_today 这个 JSON 字段随时间无限膨胀
        thirty_days_ago = (now - timedelta(days=30)).date().isoformat()
        sessions_today = {k: v for k, v in sessions_today.items() if k >= thirty_days_ago}
        engagement['sessions_today'] = sessions_today

        # 最近7天：按每一天补0（否则只统计有活动的天，会系统性高估规律性）
        # frequency 的"7天数"与 consistency 的 daily_sessions 必须用同一个窗口，否则口径不一致
        last_7_days = [(now - timedelta(days=i)).date().isoformat() for i in range(7)]
        sessions_7d_list = [sessions_today.get(d, 0) for d in last_7_days]
        sessions_7d_sum = sum(sessions_7d_list)

        # 计算各项得分（sessions_today 已剪到30天，sum 即真实的近30天）
        frequency = EngagementCalculator.calculate_frequency_score(
            sessions_last_7_days=sessions_7d_sum,
            sessions_last_30_days=sum(sessions_today.values()),
        )
        
        consistency = EngagementCalculator.calculate_consistency_score(
            daily_sessions=sessions_7d_list,
        )
        
        intensity = EngagementCalculator.calculate_intensity_score(
            avg_time_per_session_minutes=session_data.get('avg_time_minutes', 0),
            avg_questions_per_session=session_data.get('avg_questions', 0),
        )
        
        overall = EngagementCalculator.calculate_overall_score(
            frequency, consistency, intensity
        )
        
        engagement.update({
            'score': overall['score'],
            'frequency': frequency,
            'consistency': consistency,
            'intensity': intensity,
            'trend': overall['trend'],
            'last_updated': now.isoformat(),
        })
        
        self.profile.engagement = engagement
        self._save_delta('engagement', old_engagement, engagement)
        
        return self._finalize()
    
    # ===== 偏好更新 =====
    
    def update_preference(
        self,
        preference_type: str,
        preference_value: str,
    ) -> dict:
        """更新学习偏好"""
        old_prefs = self.profile.learning_preferences or {}
        
        prefs = PreferenceDetector.update_preference_scores(
            old_prefs, preference_type, preference_value
        )
        
        self.profile.learning_preferences = prefs
        self._save_delta('learning_preferences', old_prefs, prefs)
        
        return self._finalize()
    
    def update_preference_from_feedback(
        self,
        feedback_type: str,
        knowledge_tag: str = '',
    ) -> dict:
        """从反馈更新偏好"""
        # 反馈类型: too_easy, too_hard, off_topic, useful
        old_prefs = self.profile.learning_preferences or {}
        
        prefs = old_prefs.copy()
        
        if feedback_type == 'too_easy':
            # 希望更难
            prefs['difficulty_preference'] = 'hard'
            prefs['preferred_difficulty'] = 'hard'
        elif feedback_type == 'too_hard':
            # 希望更简单
            prefs['difficulty_preference'] = 'easy'
            prefs['preferred_difficulty'] = 'easy'
        elif feedback_type == 'off_topic':
            # 标记不感兴趣的topic
            if 'disliked_topics' not in prefs:
                prefs['disliked_topics'] = []
            if knowledge_tag and knowledge_tag not in prefs['disliked_topics']:
                prefs['disliked_topics'].append(knowledge_tag)
        
        self.profile.learning_preferences = prefs
        self._save_delta('learning_preferences', old_prefs, prefs)
        
        return self._finalize()
    
    # ===== 学习目标更新 =====
    
    def update_goal_from_path(
        self,
        goal_description: str,
        goal_type: str = '',
        target_date: str = '',
    ) -> dict:
        """从学习路径更新目标"""
        old_goals = self.profile.learning_goals or []
        
        goals = list(old_goals)
        
        new_goal = {
            'goal_id': f"goal_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            'description': goal_description,
            'type': goal_type,
            'target_date': target_date,
            'progress': 0.0,
            'created_at': datetime.now().isoformat(),
            'updated_at': datetime.now().isoformat(),
            'status': 'active',
        }
        
        goals.append(new_goal)
        
        # 保留最近10个目标
        if len(goals) > 10:
            goals = goals[-10:]
        
        self.profile.learning_goals = goals
        self._save_delta('learning_goals', old_goals, goals)
        
        return self._finalize()
    
    def update_goal_progress(
        self,
        goal_id: str,
        progress: float,
    ) -> dict:
        """更新目标进度"""
        old_goals = self.profile.learning_goals or []
        
        goals = old_goals.copy()
        
        for goal in goals:
            if goal.get('goal_id') == goal_id:
                goal['progress'] = max(0.0, min(1.0, progress))
                goal['updated_at'] = datetime.now().isoformat()
                if progress >= 1.0:
                    goal['status'] = 'completed'
                    goal['completed_at'] = datetime.now().isoformat()
                break
        
        self.profile.learning_goals = goals
        self._save_delta('learning_goals', old_goals, goals)
        
        return self._finalize()
    
    # ===== 批量更新 =====
    
    def apply_batch_updates(self, updates: List[dict]) -> dict:
        """
        应用批量更新
        
        Args:
            updates: 更新列表，每个元素包含:
                - dimension: 维度名
                - action: 操作类型 (update/add/set)
                - data: 更新数据
        """
        combined_delta = {}
        
        for update in updates:
            dimension = update.get('dimension')
            action = update.get('action')
            data = update.get('data', {})
            delta = {}  # 每轮清零：否则未命中的 action 组合会误用上一轮 delta，或首轮直接 UnboundLocalError

            if dimension == 'knowledge':
                if action == 'update':
                    delta = self.update_knowledge_from_quiz(**data)
                elif action == 'set':
                    self.profile.knowledge_profile = data
            elif dimension == 'misconception':
                if action == 'add':
                    delta = self.add_misconception(**data)
                elif action == 'resolve':
                    delta = self.resolve_misconception(**data)
            elif dimension == 'cognitive_style':
                delta = self.update_cognitive_style_from_chat(**data)
            elif dimension == 'engagement':
                delta = self.update_engagement(**data)
            elif dimension == 'preference':
                delta = self.update_preference(**data)
            elif dimension == 'goal':
                if action == 'update':
                    delta = self.update_goal_from_path(**data)
                elif action == 'progress':
                    delta = self.update_goal_progress(**data)
            
            combined_delta.update(delta if delta else {})
        
        return combined_delta


def misconception_is_active(m: Any) -> bool:
    """易错点条目可能是 dict（{'concept','status',...}）或纯字符串（历史/事件流写入）。
    统一判断是否为"未解决"状态，兼容两种格式，避免读取端 AttributeError。"""
    if isinstance(m, dict):
        return str(m.get('status', 'active')) == 'active'
    return bool(str(m or '').strip())


def misconception_text(m: Any) -> str:
    """取易错点的可读文本，兼容 dict / 字符串两种格式。"""
    if isinstance(m, dict):
        return str(m.get('concept') or m.get('text') or m.get('description') or m.get('label') or '').strip()
    return str(m or '').strip()


def get_profile_summary(profile: StudentProfile) -> dict:
    """获取画像摘要"""
    return {
        'knowledge_count': len(profile.knowledge_profile or {}),
        'misconception_count': len([m for m in (profile.misconceptions or []) if misconception_is_active(m)]),
        'cognitive_style': profile.cognitive_style or 'unknown',
        'engagement_score': (profile.engagement or {}).get('score', 0),
        'goal_count': len(profile.learning_goals or []),
        'preference_count': len(profile.learning_preferences or {}),
    }
