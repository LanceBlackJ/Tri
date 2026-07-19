"""
agent_system services package

提供服务模块：
- user_llm_r1_reasoning: USER-LLM R1 动态画像推理
- emotion_recognition: 情感识别引擎
- engagement_vector: Engagement Vector 参与度评估
- memory_architecture: 记忆架构
- knowledge_tracing: 知识追踪
- error_correction: 错题检测与纠正
- educational_agent: 教育智能体集成
"""

# USER-LLM R1 模块
from .user_llm_r1_reasoning import USERLLM_R1_Full, USERLLM_R1
from .user_llm_r1_iterative import IterativeReasoning, ReasoningChainBuilder
from .user_llm_r1_vector import VectorStore, SemanticRetrieval, EventAggregator
from .user_llm_r1_multimodal import MultimodalProcessor

# 全局单例
_llm_r1_engine = None

def get_reasoning_engine():
    """获取USER-LLM R1推理引擎实例"""
    global _llm_r1_engine
    if _llm_r1_engine is None:
        _llm_r1_engine = USERLLM_R1_Full()
    return _llm_r1_engine

def build_user_profile_from_reasoning(user, query, context=None):
    """从推理构建用户画像的便捷函数"""
    engine = get_reasoning_engine()
    return engine.process_interaction(user, query, context)


from .emotion_recognition import (
    EmotionRecognitionEngine,
    OCNEmotionModel,
    EmpathyResponseGenerator,
    EmotionData,
    EmotionalState,
    EmpathyStrategy,
    get_emotion_engine,
    get_empathy_generator
)

from .engagement_vector import (
    EngagementVectorEngine,
    EngagementVector,
    CognitiveEngagementCalculator,
    EmotionalEngagementCalculator,
    BehavioralEngagementCalculator,
    get_engagement_engine
)

from .memory_architecture import (
    MemoryArchitecture,
    ShortTermMemory,
    LongTermMemory,
    WorkingMemory,
    MemoryItem,
    MemoryImportance,
    get_memory_architecture
)

from .knowledge_tracing import (
    KnowledgeTracingEngine,
    DeepKnowledgeTracing,
    SpacedRepetition,
    ConceptPrerequisiteGraph,
    KnowledgeStateRecord,
    ConceptInfo,
    get_knowledge_tracing_engine
)

from .error_correction import (
    ErrorCorrectionEngine,
    ErrorAnalyzer,
    ErrorPatternLibrary,
    AdaptiveFeedbackGenerator,
    ErrorType,
    ErrorSeverity,
    ErrorPattern,
    DetectedError,
    CorrectionFeedback,
    get_error_correction_engine,
    detect_and_correct_answer
)

from .educational_agent import (
    EducationalAgent,
    LearningMode,
    UserLearningProfile,
    get_educational_agent,
    process_student_learning,
    grade_and_feedback
)

__all__ = [
    # USER-LLM R1
    'USERLLM_R1_Full',
    'USERLLM_R1',
    'IterativeReasoning',
    'ReasoningChainBuilder',
    'VectorStore',
    'SemanticRetrieval',
    'EventAggregator',
    'MultimodalProcessor',
    'get_reasoning_engine',
    'build_user_profile_from_reasoning',
    
    # 情感识别
    'EmotionRecognitionEngine',
    'OCNEmotionModel',
    'EmpathyResponseGenerator',
    'EmotionData',
    'EmotionalState',
    'EmpathyStrategy',
    'get_emotion_engine',
    'get_empathy_generator',
    
    # Engagement Vector
    'EngagementVectorEngine',
    'EngagementVector',
    'CognitiveEngagementCalculator',
    'EmotionalEngagementCalculator',
    'BehavioralEngagementCalculator',
    'get_engagement_engine',
    
    # 记忆架构
    'MemoryArchitecture',
    'ShortTermMemory',
    'LongTermMemory',
    'WorkingMemory',
    'MemoryItem',
    'MemoryImportance',
    'get_memory_architecture',
    
    # 知识追踪
    'KnowledgeTracingEngine',
    'DeepKnowledgeTracing',
    'SpacedRepetition',
    'ConceptPrerequisiteGraph',
    'KnowledgeStateRecord',
    'ConceptInfo',
    'get_knowledge_tracing_engine',
    
    # 错题检测
    'ErrorCorrectionEngine',
    'ErrorAnalyzer',
    'ErrorPatternLibrary',
    'AdaptiveFeedbackGenerator',
    'ErrorType',
    'ErrorSeverity',
    'ErrorPattern',
    'DetectedError',
    'CorrectionFeedback',
    'get_error_correction_engine',
    'detect_and_correct_answer',
    
    # 教育智能体
    'EducationalAgent',
    'LearningMode',
    'UserLearningProfile',
    'get_educational_agent',
    'process_student_learning',
    'grade_and_feedback',
]
