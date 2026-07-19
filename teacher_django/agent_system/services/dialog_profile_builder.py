"""
对话式学习画像自主构建服务

根据赛题需求：
1. 摒弃传统繁琐表单，支持通过自然语言对话自动抽取特征
2. 构建包含不少于6个维度的动态学生画像
3. 支持画像的随学随新

六个画像维度：
1. knowledge_profile - 知识基础（知识点掌握度）
2. cognitive_style - 认知风格（视觉型/听觉型/动手型/分析型/整体型）
3. misconceptions - 易错点（知识短板）
4. learning_goals - 学习目标（专业方向、学习目的）
5. learning_preferences - 学习偏好（内容格式、难度偏好、交互模式）
6. engagement - 学习参与度（频率、规律性、强度）
"""
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from django.contrib.auth import get_user_model

from ..models import StudentProfile

logger = logging.getLogger(__name__)
User = get_user_model()


class ProfileDimension:
    """画像维度定义"""
    KNOWLEDGE_PROFILE = 'knowledge_profile'
    COGNITIVE_STYLE = 'cognitive_style'
    MISCONCEPTIONS = 'misconceptions'
    LEARNING_GOALS = 'learning_goals'
    LEARNING_PREFERENCES = 'learning_preferences'
    ENGAGEMENT = 'engagement'
    
    ALL = [KNOWLEDGE_PROFILE, COGNITIVE_STYLE, MISCONCEPTIONS, LEARNING_GOALS, LEARNING_PREFERENCES, ENGAGEMENT]


class CognitiveStyleDetector:
    """认知风格检测器"""
    
    STYLE_KEYWORDS = {
        'visual': [
            '图', '图表', '图示', '可视化', '图片', '颜色', '布局', '视觉',
            '画', '截图', '示意图', '思维导图', '流程图', '结构图'
        ],
        'auditory': [
            '听', '说', '讨论', '讲解', '口述', '音频', '语音', '视频讲解',
            '听讲解', '听老师讲', '听课程', '语音讲解', '讲解视频'
        ],
        'kinesthetic': [
            '练习', '实践', '动手', '操作', '做', '实验', '编程', '写代码',
            '实操', '实战', '演练', '动手做', '实践项目', '代码练习'
        ],
        'analytical': [
            '原理', '证明', '推导', '逻辑', '分析', '为什么', '本质', '深入',
            '理论', '公式', '定理', '证明过程', '逻辑推理', '数学推导'
        ],
        'holistic': [
            '框架', '概览', '整体', '全局', '大纲', '结构', '总结', '全貌',
            '体系', '整体架构', '知识体系', '章节框架', '整体把握'
        ],
    }
    
    STYLE_DESCRIPTIONS = {
        'visual': '视觉型学习者，倾向通过图像、图表、思维导图等可视化方式学习',
        'auditory': '听觉型学习者，倾向通过讲解、讨论、音频等听觉方式学习',
        'kinesthetic': '动手型学习者，倾向通过实践、练习、编程实操等方式学习',
        'analytical': '分析型学习者，倾向深入理解原理、证明、逻辑推导',
        'holistic': '整体型学习者，倾向把握整体框架、知识体系和结构',
        'mixed': '混合型学习者，综合多种学习方式',
    }
    
    @classmethod
    def detect_style(cls, text: str) -> Tuple[str, float]:
        """
        从文本中检测认知风格
        
        Returns:
            (style_name, confidence)
        """
        if not text:
            return 'mixed', 0.0
        
        style_scores = {style: 0 for style in cls.STYLE_KEYWORDS}
        
        for style, keywords in cls.STYLE_KEYWORDS.items():
            for keyword in keywords:
                if keyword in text:
                    style_scores[style] += 1
        
        total = sum(style_scores.values())
        if total == 0:
            return 'mixed', 0.0
        
        max_score = max(style_scores.values())
        dominant_styles = [s for s, c in style_scores.items() if c == max_score]
        
        if len(dominant_styles) > 1:
            return 'mixed', max_score / total
        
        return dominant_styles[0], max_score / total


class KnowledgeExtractor:
    """知识基础抽取器"""
    
    # 常见学科领域关键词
    DOMAIN_KEYWORDS = {
        'math': ['数学', '微积分', '线性代数', '概率', '统计', '函数', '极限', '导数', '积分'],
        'cs': ['编程', '代码', '算法', '数据结构', '计算机', '软件', 'Python', 'Java', 'C++'],
        'ai': ['人工智能', '机器学习', '深度学习', '神经网络', '模型', '训练', '推理'],
        'data': ['数据', '数据库', 'SQL', '数据分析', '数据挖掘', '大数据'],
        'network': ['网络', '协议', 'TCP/IP', 'HTTP', '计算机网络'],
        'os': ['操作系统', 'Linux', 'Windows', '进程', '线程', '内存'],
        'electronics': ['电路', '电子', '芯片', '嵌入式', '硬件', '单片机'],
        'signal': ['信号', '系统', 'DSP', '傅里叶', '滤波', '通信'],
    }
    
    # 知识水平关键词
    LEVEL_KEYWORDS = {
        'beginner': ['零基础', '入门', '初学', '新手', '刚开始', '不太会', '不太懂'],
        'intermediate': ['了解', '熟悉', '掌握基础', '基本会', '有一定基础'],
        'advanced': ['熟练', '精通', '深入理解', '掌握', '擅长', '能手'],
    }
    
    @classmethod
    def extract_domains(cls, text: str) -> List[str]:
        """从文本中提取学科领域"""
        domains = []
        for domain, keywords in cls.DOMAIN_KEYWORDS.items():
            for keyword in keywords:
                if keyword in text:
                    domains.append(domain)
                    break
        return list(set(domains))
    
    @classmethod
    def extract_knowledge_level(cls, text: str) -> str:
        """从文本中提取知识水平：按各等级关键词的命中数量（证据强度）判定，
        而不是按 dict 顺序返回第一个命中（否则同时含"入门"和"熟练"会永远判 beginner）。"""
        counts = {level: sum(1 for kw in keywords if kw in text)
                  for level, keywords in cls.LEVEL_KEYWORDS.items()}
        if not any(counts.values()):
            return 'intermediate'
        return max(counts, key=lambda lv: counts[lv])
    
    @classmethod
    def extract_knowledge_tags(cls, text: str) -> List[str]:
        """从文本中提取具体知识点标签"""
        tags = []
        
        # 从DOMAIN_KEYWORDS中提取具体关键词作为标签
        for domain, keywords in cls.DOMAIN_KEYWORDS.items():
            for keyword in keywords:
                if keyword in text:
                    tags.append(keyword)
        
        return list(set(tags))


class GoalExtractor:
    """学习目标抽取器"""
    
    GOAL_TYPES = {
        'exam': ['考研', '考试', '备考', '期末', '考博', '证书', '资格证', '认证'],
        'skill': ['技能', '掌握', '学会', '精通', '掌握技术', '提升能力'],
        'knowledge': ['了解', '学习', '掌握知识', '理解', '研究', '学术'],
        'career': ['工作', '就业', '职业', '求职', '面试', '职场'],
        'project': ['项目', '实战', '实践', '做项目', '完成项目'],
    }
    
    @classmethod
    def extract_goals(cls, text: str) -> List[Dict[str, Any]]:
        """从文本中提取学习目标"""
        goals = []
        
        for goal_type, keywords in cls.GOAL_TYPES.items():
            for keyword in keywords:
                if keyword in text:
                    goal = {
                        'type': goal_type,
                        'description': keyword,
                        'confidence': 0.7,
                        'extracted_from': text[:100],
                        'created_at': datetime.now().isoformat(),
                    }
                    goals.append(goal)
        
        return goals


class PreferenceExtractor:
    """学习偏好抽取器"""
    
    CONTENT_FORMAT_KEYWORDS = {
        'text': ['文字', '文档', '阅读', '说明', '文章', '文档资料'],
        'diagram': ['图', '图表', '图示', '可视化', '思维导图', '流程图'],
        'video': ['视频', '动画', '演示', '视频讲解', '教学视频'],
        'code': ['代码', '编程', '实现', '示例', '代码示例', '编程练习'],
        'exercise': ['练习', '做题', '测试', '习题', '练习题'],
        'audio': ['音频', '播客', '听书', '语音讲解'],
    }
    
    DIFFICULTY_KEYWORDS = {
        'easy': ['简单', '基础', '入门', '浅显', '初级'],
        'medium': ['适中', '中等', '标准', '一般'],
        'hard': ['难', '挑战', '高级', '深入', '进阶'],
    }
    
    INTERACTION_MODE_KEYWORDS = {
        'active': ['提问', '讨论', '互动', '交流', '主动'],
        'passive': ['听讲', '观看', '阅读', '被动', '听课'],
    }
    
    SESSION_LENGTH_KEYWORDS = {
        'short': ['短时间', '快速', '10分钟', '20分钟', '半小时'],
        'medium': ['一小时', '半天', '适中', '常规'],
        'long': ['长时间', '一整天', '深入', '系统'],
    }
    
    @classmethod
    def extract_content_format(cls, text: str) -> List[str]:
        """提取内容格式偏好"""
        formats = []
        for fmt, keywords in cls.CONTENT_FORMAT_KEYWORDS.items():
            for keyword in keywords:
                if keyword in text:
                    formats.append(fmt)
                    break
        return list(set(formats))
    
    @classmethod
    def extract_difficulty_preference(cls, text: str) -> str:
        """提取难度偏好"""
        for diff, keywords in cls.DIFFICULTY_KEYWORDS.items():
            for keyword in keywords:
                if keyword in text:
                    return diff
        return 'medium'
    
    @classmethod
    def extract_interaction_mode(cls, text: str) -> str:
        """提取交互模式偏好"""
        for mode, keywords in cls.INTERACTION_MODE_KEYWORDS.items():
            for keyword in keywords:
                if keyword in text:
                    return mode
        return 'active'
    
    @classmethod
    def extract_session_length(cls, text: str) -> str:
        """提取偏好学习时长"""
        for length, keywords in cls.SESSION_LENGTH_KEYWORDS.items():
            for keyword in keywords:
                if keyword in text:
                    return length
        return 'medium'


class MisconceptionDetector:
    """易错点检测器"""
    
    CONFUSION_KEYWORDS = [
        '不懂', '困惑', '不清楚', '不会', '不明白', '搞不懂', '混淆',
        '难理解', '总是错', '容易错', '经常错', '不太清楚', '不太明白',
        '不太懂', '不太会', '不理解', '理解不了', '没懂', '没明白',
        '总是做错', '老是错', '一直错', '反复错', '不太清楚',
    ]
    
    @classmethod
    def detect_confusion(cls, text: str) -> bool:
        """检测是否有困惑信号"""
        for keyword in cls.CONFUSION_KEYWORDS:
            if keyword in text:
                return True
        return False
    
    @classmethod
    def extract_confusion_topic(cls, text: str) -> Optional[str]:
        """提取困惑的主题"""
        # 简单的模式匹配，提取可能的知识点
        topic_patterns = [
            r'(什么是|什么叫|如何理解|怎么理解)\s*([一-龥\w]+)',
            r'(不懂|困惑|不清楚)\s*(.+?)(吗|\?|。|，)',
            r'(为什么|怎么)\s*(.+?)(呢|\?|。)',
        ]
        
        for pattern in topic_patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(2).strip()
        
        return None


class DialogProfileBuilder:
    """
    对话式画像构建器
    
    通过自然语言对话自动抽取特征，构建动态学生画像
    """
    
    def __init__(self, user):
        self.user = user
        self.profile, _ = StudentProfile.objects.get_or_create(user=user)
        self._extracted_features = {}
    
    def analyze_message(self, message: str) -> Dict[str, Any]:
        """
        分析单条消息，抽取画像特征
        
        Returns:
            抽取的特征字典
        """
        features = {}
        
        # 1. 提取认知风格
        style, confidence = CognitiveStyleDetector.detect_style(message)
        if confidence > 0.3:
            features['cognitive_style'] = {
                'style': style,
                'confidence': confidence,
                'description': CognitiveStyleDetector.STYLE_DESCRIPTIONS.get(style, ''),
            }
        
        # 2. 提取知识基础
        domains = KnowledgeExtractor.extract_domains(message)
        level = KnowledgeExtractor.extract_knowledge_level(message)
        tags = KnowledgeExtractor.extract_knowledge_tags(message)
        if domains or tags:
            features['knowledge_profile'] = {
                'domains': domains,
                'level': level,
                'tags': tags,
            }
        
        # 3. 提取学习目标
        goals = GoalExtractor.extract_goals(message)
        if goals:
            features['learning_goals'] = goals
        
        # 4. 提取学习偏好
        content_formats = PreferenceExtractor.extract_content_format(message)
        difficulty = PreferenceExtractor.extract_difficulty_preference(message)
        interaction_mode = PreferenceExtractor.extract_interaction_mode(message)
        session_length = PreferenceExtractor.extract_session_length(message)
        if content_formats or difficulty or interaction_mode or session_length:
            features['learning_preferences'] = {
                'content_formats': content_formats,
                'difficulty_preference': difficulty,
                'interaction_mode': interaction_mode,
                'session_length': session_length,
            }
        
        # 5. 检测易错点/困惑
        has_confusion = MisconceptionDetector.detect_confusion(message)
        confusion_topic = MisconceptionDetector.extract_confusion_topic(message)
        if has_confusion or confusion_topic:
            features['misconceptions'] = {
                'has_confusion': has_confusion,
                'topic': confusion_topic,
                'source_message': message[:100],
            }
        
        self._extracted_features = features
        return features
    
    def update_profile(self, features: Dict[str, Any]) -> Dict[str, Any]:
        """
        根据提取的特征更新画像
        
        Returns:
            更新的变化量
        """
        delta = {}
        
        # 更新认知风格（写回阈值与 analyze_message 的抽取阈值(0.3)对齐，
        # 否则 0.3~0.5 的认知风格被抽出来却又被静默丢弃）
        if 'cognitive_style' in features:
            style_info = features['cognitive_style']
            if style_info['confidence'] > 0.3:
                old_style = self.profile.cognitive_style
                self.profile.cognitive_style = style_info['style']
                delta['cognitive_style'] = {
                    'before': old_style,
                    'after': style_info['style'],
                    'confidence': style_info['confidence'],
                }
        
        # 更新知识基础
        if 'knowledge_profile' in features:
            knowledge_info = features['knowledge_profile']
            current_knowledge = self.profile.knowledge_profile or {}
            
            # 合并知识点标签
            new_tags = knowledge_info.get('tags', [])
            for tag in new_tags:
                if tag not in current_knowledge:
                    current_knowledge[tag] = 0.5  # 默认掌握度0.5
            
            # 更新领域信息
            if knowledge_info.get('domains'):
                current_knowledge['__domains__'] = knowledge_info['domains']
            
            # 更新水平信息
            if knowledge_info.get('level'):
                current_knowledge['__level__'] = knowledge_info['level']
            
            self.profile.knowledge_profile = current_knowledge
            delta['knowledge_profile'] = {
                'added_tags': new_tags,
                'domains': knowledge_info.get('domains'),
            }
        
        # 更新学习目标
        if 'learning_goals' in features:
            new_goals = features['learning_goals']  # GoalExtractor 产出的是 dict 列表
            current_goals = list(self.profile.learning_goals or [])

            # 统一以「字符串(目标描述)」存储，与桥接/解析/事件路径一致，避免 learning_goals 里
            # dict 与 str 混排（会让 R1 的 set(goals) 抛 unhashable、显示端渲染出 dict repr）。
            def _goal_text(g):
                if isinstance(g, dict):
                    return str(g.get('description') or g.get('type') or '').strip()
                return str(g or '').strip()

            existing_texts = {_goal_text(g) for g in current_goals}
            added = 0
            for goal in new_goals:
                desc = _goal_text(goal)
                if desc and desc not in existing_texts:
                    current_goals.append(desc)
                    existing_texts.add(desc)
                    added += 1

            # 保留最近10个目标
            if len(current_goals) > 10:
                current_goals = current_goals[-10:]

            self.profile.learning_goals = current_goals
            delta['learning_goals'] = {
                'added_count': added,
                'total_count': len(current_goals),
            }
        
        # 更新学习偏好
        if 'learning_preferences' in features:
            prefs_info = features['learning_preferences']
            current_prefs = self.profile.learning_preferences or {}
            
            # 合并内容格式偏好
            if prefs_info.get('content_formats'):
                current_prefs['content_formats'] = list(set(
                    current_prefs.get('content_formats', []) + prefs_info['content_formats']
                ))
            
            # 更新难度偏好
            if prefs_info.get('difficulty_preference'):
                current_prefs['difficulty_preference'] = prefs_info['difficulty_preference']
            
            # 更新交互模式
            if prefs_info.get('interaction_mode'):
                current_prefs['interaction_mode'] = prefs_info['interaction_mode']
            
            # 更新学习时长偏好
            if prefs_info.get('session_length'):
                current_prefs['session_length'] = prefs_info['session_length']
            
            self.profile.learning_preferences = current_prefs
            delta['learning_preferences'] = {
                'updated_fields': list(prefs_info.keys()),
            }
        
        # 更新易错点
        if 'misconceptions' in features:
            miscon_info = features['misconceptions']
            current_misconceptions = self.profile.misconceptions or []
            
            if miscon_info.get('topic'):
                # 检查是否已存在该主题的易错点
                existing = None
                for m in current_misconceptions:
                    if m.get('tag') == miscon_info['topic']:
                        existing = m
                        break
                
                if existing:
                    # 更新现有易错点
                    existing['wrong_count'] = existing.get('wrong_count', 0) + 1
                    existing['last_wrong_at'] = datetime.now().isoformat()
                    existing['consecutive_wrong'] = existing.get('consecutive_wrong', 0) + 1
                else:
                    # 创建新易错点
                    new_miscon = {
                        'tag': miscon_info['topic'],
                        'wrong_count': 1,
                        'consecutive_wrong': 1,
                        'first_wrong_at': datetime.now().isoformat(),
                        'last_wrong_at': datetime.now().isoformat(),
                        'status': 'potential',  # 潜在易错点
                        'source_message': miscon_info['source_message'],
                    }
                    current_misconceptions.append(new_miscon)
            
            self.profile.misconceptions = current_misconceptions
            delta['misconceptions'] = {
                'has_confusion': miscon_info.get('has_confusion'),
                'topic': miscon_info.get('topic'),
            }
        
        # 保存更新
        self.profile.save()
        return delta
    
    def build_from_dialog(self, messages: List[str]) -> Dict[str, Any]:
        """
        从对话历史构建画像
        
        Args:
            messages: 对话消息列表
        
        Returns:
            综合画像特征
        """
        all_features = {}
        
        for message in messages:
            features = self.analyze_message(message)
            # 合并特征
            for key, value in features.items():
                if key not in all_features:
                    all_features[key] = value
                else:
                    # 简单合并逻辑（校验已存值类型，避免 dict.update(非dict) / list+非list 崩溃）
                    if isinstance(value, dict) and isinstance(all_features.get(key), dict):
                        all_features[key].update(value)
                    elif isinstance(value, list) and isinstance(all_features.get(key), list):
                        all_features[key] = list(set(all_features[key] + value))
                    else:
                        all_features[key] = value
        
        # 更新画像
        delta = self.update_profile(all_features)
        return {
            'features': all_features,
            'delta': delta,
        }
    
    def get_profile_summary(self) -> Dict[str, Any]:
        """获取画像摘要"""
        knowledge = self.profile.knowledge_profile or {}
        prefs = self.profile.learning_preferences or {}
        misc = self.profile.misconceptions or []
        # 易错点条目可能是 dict 或字符串，统一判断状态，避免 AttributeError
        def _active(m):
            return str(m.get('status', 'active')) == 'active' if isinstance(m, dict) else bool(str(m or '').strip())
        def _unresolved(m):
            return str(m.get('status', 'active')) != 'resolved' if isinstance(m, dict) else bool(str(m or '').strip())
        return {
            'knowledge_profile': {
                'domains': knowledge.get('__domains__', []),
                'level': knowledge.get('__level__', 'unknown'),
                'tag_count': len([k for k in knowledge.keys() if not k.startswith('__')]),
            },
            'cognitive_style': {
                'style': self.profile.cognitive_style or 'mixed',
                'description': CognitiveStyleDetector.STYLE_DESCRIPTIONS.get(
                    self.profile.cognitive_style or 'mixed', ''
                ),
            },
            'learning_goals': {
                'count': len(self.profile.learning_goals or []),
                'goals': self.profile.learning_goals or [],
            },
            'learning_preferences': {
                'content_formats': prefs.get('content_formats', []),
                'difficulty': prefs.get('difficulty_preference', 'medium'),
                'interaction_mode': prefs.get('interaction_mode', 'active'),
            },
            'misconceptions': {
                'count': len([m for m in misc if _unresolved(m)]),
                'active': [m for m in misc if _active(m)],
            },
            'engagement': {
                'score': (self.profile.engagement or {}).get('score', 0),
                'trend': (self.profile.engagement or {}).get('trend', 'stable'),
            },
        }


class ProfileBuildingPromptGenerator:
    """
    画像构建提示词生成器
    
    生成用于引导用户提供画像信息的对话提示
    """
    
    INITIAL_PROMPTS = [
        '你好！为了给你提供更个性化的学习服务，我想了解一些关于你的情况。请问你目前学习的专业或感兴趣的领域是什么？',
        '很高兴认识你！为了更好地帮助你学习，可以告诉我你的学习目标吗？比如是准备考试、掌握技能还是提升能力？',
        '你好！为了定制专属的学习方案，我需要了解一些信息。你目前的知识基础如何？是零基础、有一定基础还是比较熟练？',
        '嗨！为了给你推荐合适的学习资源，可以说说你更喜欢哪种学习方式吗？比如看视频、阅读文档、动手练习还是其他？',
    ]
    
    FOLLOW_UP_PROMPTS = {
        'knowledge': [
            '你目前在学习哪些具体的课程或知识点呢？',
            '有没有特别感兴趣或者想要深入学习的技术领域？',
            '你觉得自己在哪些知识领域比较擅长，哪些还需要加强？',
        ],
        'goals': [
            '你学习的主要目的是什么呢？比如是为了考试、工作还是兴趣？',
            '有没有短期或长期的学习目标想要达成？',
            '你希望通过学习获得什么样的能力或成果？',
        ],
        'preferences': [
            '你更喜欢通过什么方式学习？视频、文档、练习还是互动讨论？',
            '你觉得什么样难度的内容更适合你？基础、适中还是有挑战性的？',
            '你通常一次学习多长时间？有没有偏好的学习节奏？',
        ],
        'cognitive': [
            '当你遇到复杂概念时，更喜欢看图示讲解还是文字说明？',
            '学习新知识时，你更倾向于先了解整体框架还是深入细节？',
            '你觉得通过实践操作学习更有效，还是通过理论讲解更有效？',
        ],
    }
    
    @classmethod
    def get_initial_prompt(cls) -> str:
        """获取初始画像构建提示"""
        import random
        return random.choice(cls.INITIAL_PROMPTS)
    
    @classmethod
    def get_follow_up_prompt(cls, dimension: str) -> str:
        """获取指定维度的跟进提示"""
        import random
        prompts = cls.FOLLOW_UP_PROMPTS.get(dimension, [])
        if prompts:
            return random.choice(prompts)
        return '你还有什么想告诉我的吗？'


def extract_profile_from_dialog(user, messages: List[str]) -> Dict[str, Any]:
    """
    从对话消息中提取用户画像
    
    Args:
        user: 用户对象
        messages: 对话消息列表
    
    Returns:
        提取的画像特征
    """
    builder = DialogProfileBuilder(user)
    return builder.build_from_dialog(messages)


def update_profile_from_single_message(user, message: str) -> Dict[str, Any]:
    """
    从单条消息更新用户画像（随学随新）
    
    Args:
        user: 用户对象
        message: 单条消息
    
    Returns:
        更新的变化量
    """
    builder = DialogProfileBuilder(user)
    features = builder.analyze_message(message)
    return builder.update_profile(features)


def get_profile_summary_for_user(user) -> Dict[str, Any]:
    """获取用户画像摘要"""
    builder = DialogProfileBuilder(user)
    return builder.get_profile_summary()
