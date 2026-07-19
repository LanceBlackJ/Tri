"""
画像信号采集器 - 统一接收各类用户行为信号
"""
import logging
import threading
from datetime import datetime
from typing import Optional

from django.contrib.auth import get_user_model
from django.utils import timezone

from ..models import ProfileEvent, StudentProfile

logger = logging.getLogger(__name__)
User = get_user_model()


class ProfileSignalType:
    # ===== 做题相关 =====
    QUIZ_CORRECT = "quiz_correct"
    QUIZ_WRONG = "quiz_wrong"
    QUIZ_CONSECUTIVE_WRONG = "quiz_consecutive_wrong"
    QUIZ_SKIPPED = "quiz_skipped"
    QUIZ_FEEDBACK = "quiz_feedback"
    QUIZ_TIME_SPENT = "quiz_time_spent"

    # ===== 对话相关 =====
    CHAT_QUESTION_TYPE = "chat_question_type"
    CHAT_EXPLANATION_REQUEST = "chat_explanation_request"
    CHAT_PRACTICE_REQUEST = "chat_practice_request"
    CHAT_CONFUSION_SIGNAL = "chat_confusion_signal"

    # ===== 学习行为 =====
    LEARNING_PATH_STARTED = "learning_path_started"
    LEARNING_PATH_COMPLETED = "learning_path_completed"
    LEARNING_MATERIAL_VIEWED = "learning_material_viewed"
    LEARNING_SESSION_END = "learning_session_end"

    # ===== 偏好反馈 =====
    PREFERENCE_EXPLICIT = "preference_explicit"
    PREFERENCE_IMPLICIT = "preference_implicit"

    # ===== 画像维度 =====
    KNOWLEDGE_UPDATED = "knowledge_updated"
    MISCONCEPTION_ADDED = "misconception_added"
    COGNITIVE_STYLE_UPDATED = "cognitive_style_updated"
    ENGAGEMENT_UPDATED = "engagement_updated"
    PREFERENCE_UPDATED = "preference_updated"
    GOAL_UPDATED = "goal_updated"


class ProfileSignalCollector:
    """
    统一信号采集器
    
    使用方式:
        ProfileSignalCollector.emit(
            user=request.user,
            signal_type=ProfileSignalType.QUIZ_WRONG,
            trigger_source="material_quiz",
            data={
                "attempt_id": 123,
                "knowledge_tag": "函数极限",
                "is_correct": False,
            }
        )
    """
    
    _queue = []
    _queue_lock = threading.Lock()
    # 说明：原设计想用定时器批处理，但定时器从未启动，导致不足阈值的信号一直悬在进程内存里、
    # 重启即丢，多 worker 下更难达阈值——"随学随新"因此不可靠。这些信号本就低频，
    # 直接把触发阈值设为 1（每条即刻落库），保证不丢，代价可忽略。
    _BATCH_SIZE_TRIGGER = 1
    
    @classmethod
    def emit(
        cls,
        user,
        signal_type: str,
        trigger_source: str,
        data: dict,
        course_id: Optional[int] = None,
        material_id: Optional[int] = None,
        dedupe_key: Optional[str] = None,
    ):
        """
        发射信号到队列
        
        Args:
            user: 用户对象
            signal_type: 信号类型
            trigger_source: 触发来源标识
            data: 信号数据
            course_id: 关联课程ID
            material_id: 关联资料ID
            dedupe_key: 去重键，用于防止重复处理
        """
        if not user or not user.is_authenticated:
            return
        
        signal = {
            'user_id': user.id,
            'event_type': signal_type,
            'source_app': trigger_source,
            'course_id': course_id,
            'material_id': material_id,
            'payload': data,
            'dedupe_key': dedupe_key or '',
            'created_at': datetime.now().isoformat(),
        }
        
        with cls._queue_lock:
            cls._queue.append(signal)
            queue_size = len(cls._queue)
        
        # 达到批量阈值或队列已满，立即处理
        if queue_size >= cls._BATCH_SIZE_TRIGGER or queue_size >= 100:
            cls._process_batch()
        
        logger.debug(f"信号入队: {signal_type} from {trigger_source}, 队列长度: {queue_size}")
    
    @classmethod
    def _process_batch(cls):
        """处理队列中的所有信号"""
        with cls._queue_lock:
            if not cls._queue:
                return
            batch = cls._queue[:]
            cls._queue = []
        
        logger.info(f"开始处理画像信号批量任务，共 {len(batch)} 条")
        
        for signal in batch:
            try:
                cls._process_single_signal(signal)
            except Exception as e:
                logger.exception(f"处理画像信号失败: {e}, signal={signal}")
    
    @classmethod
    def _process_single_signal(cls, signal: dict):
        """处理单条信号"""
        user_id = signal['user_id']
        event_type = signal['event_type']
        payload = signal['payload']
        
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            logger.warning(f"用户不存在: {user_id}")
            return

        # 去重：同一 dedupe_key 已处理过就跳过，避免掌握度被重复加减（双计）
        dedupe_key = signal.get('dedupe_key', '')
        if dedupe_key and ProfileEvent.objects.filter(user=user, dedupe_key=dedupe_key).exists():
            logger.debug(f"跳过重复信号 dedupe_key={dedupe_key}")
            return

        # 获取当前画像快照
        profile_before = cls._get_profile_snapshot(user)
        
        # 根据信号类型更新画像
        delta = cls._dispatch_update(user, event_type, payload)
        
        # 记录事件
        try:
            ProfileEvent.objects.create(
                user=user,
                event_type=event_type,
                source_app=signal['source_app'],
                course_id=signal.get('course_id'),
                material_id=signal.get('material_id'),
                payload=payload,
                profile_delta=delta or {},
                dedupe_key=signal.get('dedupe_key', ''),
                processed_at=timezone.now(),
            )
        except Exception as e:
            logger.exception(f"创建 ProfileEvent 失败: {e}")
    
    @classmethod
    def _get_profile_snapshot(cls, user) -> dict:
        """获取用户画像快照"""
        try:
            profile = user.student_profile
            return {
                'knowledge_profile': profile.knowledge_profile or {},
                'cognitive_style': profile.cognitive_style or '',
                'learning_goals': profile.learning_goals or [],
                'misconceptions': profile.misconceptions or [],
                'engagement': profile.engagement or {},
                'learning_preferences': profile.learning_preferences or {},
            }
        except StudentProfile.DoesNotExist:
            return {}
    
    @classmethod
    def _dispatch_update(cls, user, event_type: str, payload: dict) -> dict:
        """根据信号类型分发到对应的更新器"""
        from .profile_auto_updater import ProfileAutoUpdater
        from .dialog_profile_builder import update_profile_from_single_message
        
        updater = ProfileAutoUpdater(user)
        
        if event_type == ProfileSignalType.QUIZ_CORRECT:
            return updater.update_knowledge_from_quiz(
                knowledge_tags=payload.get('knowledge_tags', []),
                is_correct=True,
                difficulty=payload.get('difficulty', 'standard'),
            )
        elif event_type == ProfileSignalType.QUIZ_WRONG:
            return updater.update_knowledge_from_quiz(
                knowledge_tags=payload.get('knowledge_tags', []),
                is_correct=False,
                difficulty=payload.get('difficulty', 'standard'),
            )
        elif event_type == ProfileSignalType.QUIZ_CONSECUTIVE_WRONG:
            return updater.add_misconception(
                knowledge_tag=payload.get('knowledge_tag', ''),
                wrong_details=payload.get('wrong_details', ''),
            )
        elif event_type == ProfileSignalType.QUIZ_FEEDBACK:
            return updater.update_preference_from_feedback(
                feedback_type=payload.get('feedback_type', ''),
                knowledge_tag=payload.get('knowledge_tag', ''),
            )
        elif event_type == ProfileSignalType.CHAT_QUESTION_TYPE:
            # 使用对话式画像构建器分析消息
            text = payload.get('text', '')
            if text:
                # 先用新的对话式画像构建器更新
                dialog_delta = update_profile_from_single_message(user, text)
                # 再用原有的认知风格推断做补充
                try:
                    updater.update_cognitive_style_from_chat(
                        question_type=payload.get('question_type', ''),
                    )
                except Exception:
                    pass
                return dialog_delta
            return updater.update_cognitive_style_from_chat(
                question_type=payload.get('question_type', ''),
            )
        elif event_type == ProfileSignalType.CHAT_CONFUSION_SIGNAL:
            return updater.add_confusion_signal(
                topic=payload.get('topic', ''),
                question_text=payload.get('question_text', ''),
            )
        elif event_type == ProfileSignalType.LEARNING_SESSION_END:
            return updater.update_engagement(
                session_data=payload,
            )
        elif event_type == ProfileSignalType.LEARNING_PATH_STARTED:
            return updater.update_goal_from_path(
                goal_description=payload.get('goal_description', ''),
                goal_type=payload.get('goal_type', ''),
            )
        elif event_type == ProfileSignalType.PREFERENCE_EXPLICIT:
            return updater.update_preference(
                preference_type=payload.get('preference_type', ''),
                preference_value=payload.get('preference_value', ''),
            )
        
        return {}
    
    @classmethod
    def flush(cls):
        """强制刷新队列"""
        cls._process_batch()
    
    @classmethod
    def get_queue_size(cls) -> int:
        """获取队列当前大小"""
        with cls._queue_lock:
            return len(cls._queue)


# 便捷函数
def emit_profile_signal(user, signal_type: str, trigger_source: str, **kwargs):
    """发射画像信号的便捷函数"""
    ProfileSignalCollector.emit(
        user=user,
        signal_type=signal_type,
        trigger_source=trigger_source,
        data=kwargs,
        course_id=kwargs.pop('course_id', None),
        material_id=kwargs.pop('material_id', None),
        dedupe_key=kwargs.pop('dedupe_key', None),
    )
