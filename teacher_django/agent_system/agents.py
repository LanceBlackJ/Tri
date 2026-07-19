"""多智能体协同编排与示例 agent 实现（基于讯飞星火）"""
import logging
import math
import re
from typing import Optional
from django.utils import timezone
from django.utils.dateparse import parse_datetime
import json

from .services.xinghuo_client import XinghuoClient
from .services.safety import check_text, censor_text, check_with_xinghuo
from .models import LearningResource, AgentTask

logger = logging.getLogger(__name__)


def get_user_profile_dict(user) -> dict:
    """读取该用户的6维学习画像（StudentProfile），转换为各 Agent 通用的 profile dict。

    冷启动用户（无任何画像信号）返回空 dict，调用方应据此跳过个性化逻辑。
    """
    try:
        profile = user.student_profile
    except Exception:
        return {}

    if not (profile.knowledge_profile or profile.cognitive_style or profile.learning_goals
            or profile.misconceptions or profile.learning_preferences):
        return {}

    return {
        'knowledge_profile': profile.knowledge_profile or {},
        'cognitive_style': profile.cognitive_style or '',
        'learning_goals': profile.learning_goals or [],
        'misconceptions': profile.misconceptions or [],
        'engagement': profile.engagement or {},
        'learning_preferences': profile.learning_preferences or {},
    }


def build_analogy_seed(knowledge_profile: dict, limit: int = 2) -> str:
    """从知识画像中挑选学生已经较好掌握的概念，作为类比讲解的"种子"
    （Analogical Scaffolding, Yasunaga et al. 2023 ICLR）。

    knowledge_profile 支持两种取值形式：
    - 字符串等级，例如 {"梯度方向": "高级", "矩阵": "中级"}
    - 结构化字典，例如 {"梯度方向": {"mastery_score": 85, ...}}
    """
    if not isinstance(knowledge_profile, dict):
        return ''

    candidates = []
    for concept, value in knowledge_profile.items():
        concept_name = str(concept).strip()
        if not concept_name or concept_name == 'overall':
            continue
        if isinstance(value, dict):
            try:
                score = float(value.get('mastery_score'))
            except (TypeError, ValueError):
                continue
            if score >= 70:
                candidates.append((score, concept_name))
        elif isinstance(value, str):
            if value == '高级':
                candidates.append((100.0, concept_name))
            elif value == '中级':
                candidates.append((70.0, concept_name))

    if not candidates:
        return ''

    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return '、'.join(name for _, name in candidates[:limit])


class BaseAgent:
    def __init__(self, user, task: Optional[AgentTask] = None, client: Optional[XinghuoClient] = None):
        self.user = user
        self.task = task
        self.client = client or XinghuoClient()

    def _safe_and_meta(self, text: str) -> tuple:
        """统一安全检查：优先使用讯飞合规检查（如果可用），否则退回本地词表检查。
        返回 (可能被审查/脱敏后的文本, meta_dict)。"""
        try:
            meta = check_with_xinghuo(text)
        except Exception:
            meta = check_text(text)
        # 兼容旧的返回结构
        if not isinstance(meta, dict):
            meta = {'safe': True, 'labels': []}
        safe_flag = meta.get('safe', True)
        if not safe_flag:
            text = censor_text(text)
        return text, meta


class ContentAgent(BaseAgent):
    """课程文档生成智能体（集成COGENT框架）"""
    
    def __init__(self, user, task: Optional[AgentTask] = None, client: Optional[XinghuoClient] = None,
                 grade_level: str = 'college'):
        super().__init__(user, task, client)
        self.grade_level = grade_level
    
    def generate_doc(self, topic: str, user_profile: dict = None) -> LearningResource:
        """生成专业课程讲解文档（集成课程标准和可读性控制）"""
        from .curriculum_standards import CurriculumStandards
        from .readability_controller import ReadabilityController
        
        # 获取课程标准
        standards_db = CurriculumStandards()
        standards = standards_db.query_standards(topic, self.grade_level)
        
        # 获取可读性约束
        readability_ctrl = ReadabilityController(self.grade_level)
        readability_prompt = readability_ctrl.build_readability_prompt()
        
        profile_context = self._build_profile_context(user_profile)
        learning_objectives = standards.get('learning_objectives', [])
        concepts = standards.get('concepts', [])
        concept_labels = [c.get('label', '') for c in concepts]
        
        prompt = (
            f"请为高校课程主题“{topic}”生成一份面向{standards.get('grade_level', '大学')}学生的详细讲解文档，要求：\n"
            "1) 以 Markdown 格式输出，章节用 '##' 标记；\n"
            "2) 每章包含关键概念、示例与一到两个练习题；\n"
            "3) 语言简洁、符合目标受众水平；\n"
            "4) 只输出文档内容，不要额外说明。\n"
            f"\n【课程标准】\n"
            f"- 学科：{standards.get('subject', '')}\n"
            f"- 学习目标：{', '.join(learning_objectives)}\n"
            f"- 核心知识点：{', '.join(concept_labels)}\n"
            f"\n{readability_prompt}\n"
            f"\n学习者画像：{profile_context}"
        )
        text = self.client.generate_text(prompt, max_tokens=3072)
        text, safe = self._safe_and_meta(text)
        
        # 执行可读性调整
        if self.grade_level in ['primary', 'junior']:
            text = readability_ctrl.adjust_readability(text, topic)
        
        res = LearningResource.objects.create(
            title=f"{topic} - 课程讲解",
            resource_type='doc',
            content=text,
            author=self.user,
            metadata={'source': 'xinghuo', 'safe': safe.get('safe', True) if isinstance(safe, dict) else bool(safe), 'profile_used': user_profile is not None,
                      'standards_aligned': True, 'grade_level': self.grade_level},
        )
        return res
    
    def _build_profile_context(self, profile: dict) -> str:
        """从用户画像构建上下文提示"""
        if not profile:
            return ""
        context = []
        if profile.get('knowledge_profile'):
            context.append(f"知识基础：{profile['knowledge_profile']}")
        if profile.get('cognitive_style'):
            context.append(f"认知风格：{profile['cognitive_style']}")
        if profile.get('learning_goals'):
            context.append(f"学习目标：{', '.join(profile['learning_goals'][:3])}")
        return "；".join(context) if context else ""


class PPTAgent(BaseAgent):
    """PPT大纲生成智能体（集成COGENT框架）"""
    
    def __init__(self, user, task: Optional[AgentTask] = None, client: Optional[XinghuoClient] = None,
                 grade_level: str = 'college'):
        super().__init__(user, task, client)
        self.grade_level = grade_level
    
    def generate_ppt(self, topic: str, user_profile: dict = None) -> LearningResource:
        """生成教学PPT大纲（集成课程标准和可读性控制）"""
        from .curriculum_standards import CurriculumStandards
        from .readability_controller import ReadabilityController
        
        # 获取课程标准
        standards_db = CurriculumStandards()
        standards = standards_db.query_standards(topic, self.grade_level)
        
        # 获取可读性约束
        readability_ctrl = ReadabilityController(self.grade_level)
        readability_prompt = readability_ctrl.build_readability_prompt()
        
        profile_context = self._build_profile_context(user_profile)
        concepts = standards.get('concepts', [])
        concept_labels = [c.get('label', '') for c in concepts]
        
        prompt = (
            f"为主题“{topic}”生成一份面向{standards.get('grade_level', '大学')}学生的教学PPT大纲，要求：\n"
            "- 每页包含页标题与 2-4 个要点；\n"
            "- 输出为清单格式，便于转换为幻灯片；\n"
            "- 包含封面、目录、章节内容、练习、总结；\n"
            "- 只输出大纲文本，不要额外解释。\n"
            f"\n【课程标准】\n"
            f"- 学科：{standards.get('subject', '')}\n"
            f"- 核心知识点：{', '.join(concept_labels)}\n"
            f"\n{readability_prompt}\n"
            f"\n学习者画像：{profile_context}"
        )
        text = self.client.generate_text(prompt)
        text, safe = self._safe_and_meta(text)
        
        res = LearningResource.objects.create(
            title=f"{topic} - PPT 大纲",
            resource_type='ppt',
            content=text,
            author=self.user,
            metadata={'source': 'xinghuo', 'safe': safe.get('safe', True) if isinstance(safe, dict) else bool(safe), 'profile_used': user_profile is not None,
                      'standards_aligned': True, 'grade_level': self.grade_level},
        )
        return res

    def _build_profile_context(self, profile: dict) -> str:
        if not profile:
            return ""
        context = []
        if profile.get('learning_preferences'):
            prefs = profile['learning_preferences']
            if prefs.get('preferred_format'):
                context.append(f"偏好格式：{prefs['preferred_format']}")
        return "；".join(context) if context else ""


class QuizAgent(BaseAgent):
    """题库生成智能体（集成COGENT框架）"""
    
    def __init__(self, user, task: Optional[AgentTask] = None, client: Optional[XinghuoClient] = None,
                 grade_level: str = 'college'):
        super().__init__(user, task, client)
        self.grade_level = grade_level
    
    def _parse_quiz_json(self, text: str, topic: str, count: int):
        import re
        
        def try_parse(s):
            s = s.strip()
            if not s:
                return None
            try:
                return json.loads(s)
            except Exception:
                pass
            try:
                m = re.search(r'\{[\s\S]*\}', s)
                if m:
                    return json.loads(m.group(0))
            except Exception:
                pass
            return None
        
        quiz_json = try_parse(text)
        
        if quiz_json is None and text:
            lines = text.split('\n')
            for line in lines:
                if '{' in line:
                    sub = line[line.index('{'):]
                    result = try_parse(sub)
                    if result:
                        quiz_json = result
                        break
        
        if quiz_json is None:
            retry_prompt = (
                f"你的上次输出格式不正确，无法解析为JSON。\n"
                f"请直接返回符合以下格式的JSON，不要任何其他文字：\n"
                f"格式要求：\n"
                f"- 外层必须是对象，包含questions数组\n"
                f"- questions数组中每个对象包含：id(数字)、type(字符串)、question(字符串)、answer(字符串)、explanation(字符串)\n"
                f"- 选择题需要options数组\n"
                f"\n"
                f"示例（请参考格式，但内容必须自己生成）：\n"
                f'{{"questions":[{{"id":1,"type":"single_choice","question":"以下哪种总线是串行总线？","options":["PCI总线","IIC总线","ISA总线","AGP总线"],"answer":"IIC总线","explanation":"IIC总线是一种简单的双向二线制同步串行总线。"}}]}}'
            )
            try:
                text2 = self.client.generate_text(retry_prompt, max_tokens=1000)
                text2, _ = self._safe_and_meta(text2)
                quiz_json = try_parse(text2)
                print(f"[AGENT DEBUG] Retry parse result: {quiz_json}")
            except Exception as e:
                print(f"[AGENT DEBUG] Retry parse error: {e}")

        if quiz_json is None:
            logger.warning('Quiz JSON parsing failed for topic %s; returning empty question set', topic)
            quiz_json = {'questions': []}
        return quiz_json

    def generate_quiz(self, topic: str, count: int = 5, user_profile: dict = None) -> LearningResource:
        """生成练习题（集成课程标准）"""
        from .curriculum_standards import CurriculumStandards
        
        # 获取课程标准
        standards_db = CurriculumStandards()
        standards = standards_db.query_standards(topic, self.grade_level)
        
        profile_context = self._build_profile_context(user_profile)
        objectives = standards.get('learning_objectives', [])
        
        prompt = (
            f"为主题“{topic}”生成{count}道练习题，面向{standards.get('grade_level', '大学')}学生，题目类型包含选择题(single_choice)与简答题(short_answer)。\n"
            "必须只输出合法的 JSON，格式如下：{\n  \"questions\": [\n    {\"id\": 1, \"type\": \"single_choice\", \"question\": \"...\", \"options\": [\"A\",\"B\"], \"answer\": \"A\", \"explanation\": \"...\"}\n  ]\n}\n"
            "不要输出任何额外文本或注释。\n"
            f"\n【课程标准】\n"
            f"- 学习目标：{', '.join(objectives)}\n"
            f"\n学习者画像：{profile_context}"
        )
        text = self.client.generate_text(prompt)
        text, safe = self._safe_and_meta(text)
        quiz_json = self._parse_quiz_json(text, topic, count)

        text_out = json.dumps(quiz_json, ensure_ascii=False)
        res = LearningResource.objects.create(
            title=f"{topic} - 练习题",
            resource_type='quiz',
            content=text_out,
            author=self.user,
            metadata={'source': 'xinghuo', 'safe': safe.get('safe', True) if isinstance(safe, dict) else bool(safe), 'profile_used': user_profile is not None,
                      'standards_aligned': True, 'grade_level': self.grade_level},
        )
        return res

    def _build_profile_context(self, profile: dict) -> str:
        if not profile:
            return ""
        context = []
        if profile.get('misconceptions'):
            context.append(f"易错点：{', '.join(profile['misconceptions'][:3])}")
        if profile.get('knowledge_profile'):
            weak_points = [k for k, v in profile['knowledge_profile'].items() if str(v).lower() in ['初级', 'low', '0', '1', '2']]
            if weak_points:
                context.append(f"薄弱知识点：{', '.join(weak_points[:3])}")
        return "；".join(context) if context else ""

    @staticmethod
    def build_material_quiz_prompt(topic: str, context_text: str, count: int = 1, question_type: str = 'mixed', variation: int = 0) -> str:
        count = max(1, int(count))
        type_instruction = ''
        if question_type == 'single_choice':
            type_instruction = '⚠️ 必须生成选择题（single_choice），包含4个选项。\n'
        elif question_type == 'short_answer':
            type_instruction = '⚠️ 必须生成简答题（short_answer），不需要选项，但必须有明确的答案和解析。\n'
        elif question_type == 'true_false':
            type_instruction = (
                '⚠️ 必须生成判断题（true_false），'
                '题目是陈述句，学生判断对错；'
                'answer只能是"正确"或"错误"，不要加任何其他内容。\n'
            )
        
        variations = [
            '请从资料中的具体技术细节出发出题。',
            '请从资料中的概念对比角度出发出题。',
            '请从资料中的实际应用场景出发出题。',
            '请从资料中的易错点和常见误解出发出题。',
            '请从资料中的原理机制出发出题。',
            '请从资料中的优缺点分析出发出题。',
            '请从资料中的发展历程出发出题。',
            '请从资料中的典型案例出发出题。',
        ]
        variation_text = variations[variation % len(variations)]
        
        return (
            f"请围绕下面提供的课程资料，为主题“{topic}”生成{count}道高质量练习题。\n"
            f"⚠️ 重要：必须严格生成{count}道题，不多不少。\n"
            f"{type_instruction}"
            "⚠️ 严禁生成'这里是一个问题？'、'选项A'、'选项B'等占位符内容！必须生成真实的、有意义的题目！\n"
            f"提示：{variation_text}\n"
            "要求：\n"
            "1. 题目必须是一个明确的问题，不能只是一个短语、口号、名言或句子；\n"
            "2. 题目应综合资料中多个页面的内容，考查学生对知识点的整体理解和关联能力；\n"
            "3. 优先出对比类、关联类、综合应用类题目，而非仅基于单个页面的细节题；\n"
            "4. 不要出‘请根据资料简述第1页核心内容’、‘请概括这一页’、‘请复述本段’这类复述型水题；\n"
            "   允许结合你已有的学科知识对题目做必要补充，但核心内容必须来自所提供的资料。\n"
            "5. 优先考查概念辨析、因果关系、机制理解、条件变化、易错点和简单应用；\n"
            "6. 选择题必须有4个有迷惑性的选项，且 answer 必须直接等于正确选项文本，不要只写 A/B/C/D；\n"
            "7. 判断题要能明确判断正误，并给出简短依据；\n"
            "8. 简答题必须有明确、可评分的标准答案，不能空泛；\n"
            "9. 每道题都要包含 answer 和 explanation 字段；\n"
            "10. 如是选择题，必须提供 options 数组，且至少有3个选项；\n"
            "11. 必须均衡覆盖资料的开头、中段和结尾部分的知识点，不要集中在资料前半部分；\n"
            "\n"
            "⚠️ 输出格式：只输出纯JSON，不要输出任何其他文字，不要用```json包裹，不要输出格式示例！\n"
            "\n"
            "正确输出示例（直接输出这样的JSON，不要包裹在代码块中）：\n"
            '{"questions":[{"id":1,"type":"single_choice","question":"以下哪种总线是串行总线？","options":["PCI总线","IIC总线","ISA总线","AGP总线"],"answer":"IIC总线","explanation":"IIC总线是一种简单的双向二线制同步串行总线，只需两根线即可传送信息。"}]}'
            "\n"
            "课程资料如下：\n"
            f"{context_text}"
        )

    def _validate_question(self, question: dict, required_type: str = 'mixed') -> bool:
        question_text = str(question.get('question') or '').strip()
        answer = str(question.get('answer') or '').strip()
        explanation = str(question.get('explanation') or '').strip()
        options = question.get('options', [])
        q_type = question.get('type', '')
        
        placeholder_patterns = ['这里填写', '这里是一个', '请填写', '选项A', '选项B', '选项C', '选项D']
        for pattern in placeholder_patterns:
            if pattern in question_text or pattern in answer or pattern in explanation:
                return False
        
        if not question_text:
            return False
        
        if len(question_text) < 5:
            return False
        
        # True/false questions are declarative statements -- skip the question-mark check
        is_true_false = q_type == 'true_false' or required_type == 'true_false'
        if not is_true_false:
            if not (question_text.endswith('？') or question_text.endswith('?') or
                    any(word in question_text for word in ['什么', '哪', '如何', '为什么', '是否', '正确', '错误', '属于', '是'])):
                return False
        
        if not answer:
            return False
        
        if not explanation:
            return False

        # For true/false: answer must be a recognizable true/false token
        if is_true_false:
            norm_answer = answer.strip().rstrip('的。！!')
            if norm_answer not in {'正确', '错误', 'true', 'false', '对', '错', '是', '否', 'T', 'F'}:
                return False

        # 检查题目类型是否符合要求
        if required_type != 'mixed':
            if required_type == 'single_choice' and q_type != 'single_choice':
                return False
            if required_type == 'short_answer' and q_type != 'short_answer':
                return False
            if required_type == 'true_false' and q_type != 'true_false':
                return False
        
        if q_type in ['single_choice', 'multiple_choice']:
            if not isinstance(options, list) or len(options) < 3:
                return False
        
        return True

    def generate_quiz_from_context(self, topic: str, context_text: str, count: int = 5, metadata: Optional[dict] = None, question_type: str = 'mixed') -> LearningResource:
        import logging
        logger = logging.getLogger(__name__)
        
        target_count = max(count, 3)
        all_questions = []
        seen_fingerprints = set()
        success_count = 0
        fail_count = 0
        duplicate_count = 0
        invalid_count = 0

        logger.info(f"Starting quiz generation: target={target_count}, question_type={question_type}")

        # 一次性生成：强模型（如 32b）一趟就能返回全部题目，不再分多轮补题。
        # 若单次返回不足 target_count，交给上层 _build_fallback_quiz_questions 补齐，避免串行多次 LLM 调用。
        for batch_round in range(1):
            remaining = target_count - len(all_questions)
            if remaining <= 0:
                break
            try:
                prompt = self.build_material_quiz_prompt(topic, context_text, remaining, question_type, batch_round)
                # 给足 token 让一趟把所有题（含选项+解析）写完，别被截断
                max_tokens = min(1200 + remaining * 650, 6000)
                text = self.client.generate_text(prompt, max_tokens=max_tokens)
                print(f"[AGENT DEBUG] Round {batch_round}: requested {remaining}, got response len={len(text) if text else 0}")
                text, safe = self._safe_and_meta(text)
                quiz_json = self._parse_quiz_json(text, topic, remaining)

                if isinstance(quiz_json, dict) and isinstance(quiz_json.get('questions'), list):
                    question_list = quiz_json['questions']
                    print(f"[AGENT DEBUG] Round {batch_round}: got {len(question_list)} questions from AI")
                    for question in question_list:
                        if len(all_questions) >= target_count:
                            break
                        if not isinstance(question, dict):
                            fail_count += 1
                            continue

                        is_valid = self._validate_question(question, question_type)
                        if not is_valid:
                            invalid_count += 1
                            logger.info(
                                f"Invalid question skipped, round={batch_round}, "
                                f"required_type={question_type}, actual_type={question.get('type')}, "
                                f"text={str(question.get('question', ''))[:60]}"
                            )
                            continue

                        fingerprint = self._build_question_fingerprint(question)
                        if not fingerprint:
                            fail_count += 1
                            continue
                        if fingerprint in seen_fingerprints:
                            duplicate_count += 1
                            continue

                        seen_fingerprints.add(fingerprint)
                        question['question_fingerprint'] = fingerprint
                        all_questions.append(question)
                        success_count += 1
                else:
                    fail_count += 1
                    logger.info(f"Parse failed, round={batch_round}")
            except Exception as e:
                fail_count += 1
                logger.error(f"Generation error: {str(e)}, round={batch_round}")

        logger.info(f"Generation complete: success={success_count}, fail={fail_count}, duplicate={duplicate_count}, invalid={invalid_count}, final={len(all_questions)}")
        
        quiz_json = {'questions': all_questions[:target_count]}
        resource_metadata = {'source': 'xinghuo', 'safe': (safe.get('safe', True) if isinstance(safe, dict) else bool(safe)) if 'safe' in locals() else True, 'generated_from': 'material_context'}
        if isinstance(metadata, dict):
            resource_metadata.update(metadata)
        return LearningResource.objects.create(
            title=f"{topic} - 资料练习题",
            resource_type='quiz',
            content=json.dumps(quiz_json, ensure_ascii=False),
            author=self.user,
            metadata=resource_metadata,
        )
    
    def _build_question_fingerprint(self, question: dict) -> str:
        import hashlib
        question_text = str(question.get('question') or '').strip()
        fingerprint_text = question_text
        return hashlib.md5(fingerprint_text.encode('utf-8')).hexdigest()[:16]


class VideoAgent(BaseAgent):
    """视频脚本生成智能体"""
    
    def generate_video_script(self, topic: str, user_profile: dict = None) -> LearningResource:
        """生成教学视频脚本"""
        profile_context = self._build_profile_context(user_profile)
        prompt = (
            f"为主题“{topic}”生成一段2-3分钟的教学短视频脚本，要求：\n"
            "- 列出分镜头（每镜头 1-2 句画面描述 + 1-2 句讲解要点）；\n"
            "- 包含开场钩子和结尾行动呼吁；\n"
            "- 语言生动，适合短视频平台传播；\n"
            "- 以清单/分镜格式输出，不要额外说明。\n"
            f"学习者画像：{profile_context}"
        )
        text = self.client.generate_text(prompt)
        text, safe = self._safe_and_meta(text)
        res = LearningResource.objects.create(
            title=f"{topic} - 视频脚本",
            resource_type='video',
            content=text,
            author=self.user,
            metadata={'source': 'xinghuo', 'safe': safe.get('safe', True) if isinstance(safe, dict) else bool(safe), 'profile_used': user_profile is not None},
        )
        return res

    def _build_profile_context(self, profile: dict) -> str:
        if not profile:
            return ""
        context = []
        if profile.get('learning_preferences'):
            prefs = profile['learning_preferences']
            if prefs.get('preferred_format') == 'video':
                context.append("偏好视频学习")
        return "；".join(context) if context else ""


class CodeAgent(BaseAgent):
    """代码案例生成智能体"""
    
    def generate_code_example(self, topic: str, language: str = 'python', user_profile: dict = None) -> LearningResource:
        """生成实操代码示例"""
        profile_context = self._build_profile_context(user_profile)
        prompt = (
            f"为主题“{topic}”生成一个实操代码示例，使用{language}语言，并包含注释与简短运行说明。\n"
            "请将代码包裹在适当的代码块标记（例如 ```python ```），并确保示例可直接复制运行。\n"
            f"学习者画像：{profile_context}"
        )
        text = self.client.generate_text(prompt)
        text, safe = self._safe_and_meta(text)
        res = LearningResource.objects.create(
            title=f"{topic} - 代码示例",
            resource_type='code',
            content=text,
            author=self.user,
            metadata={'source': 'xinghuo', 'safe': safe.get('safe', True) if isinstance(safe, dict) else bool(safe), 'profile_used': user_profile is not None, 'language': language},
        )
        return res

    def _build_profile_context(self, profile: dict) -> str:
        if not profile:
            return ""
        context = []
        if profile.get('knowledge_profile'):
            kp = profile['knowledge_profile']
            if '编程' in kp or 'Python' in kp or '代码' in kp:
                context.append(f"编程基础：{kp.get('编程', kp.get('Python', '入门'))}")
        return "；".join(context) if context else ""


class MindMapAgent(BaseAgent):
    """思维导图生成智能体"""
    
    def generate_mindmap(self, topic: str, user_profile: dict = None) -> LearningResource:
        """生成知识点思维导图"""
        profile_context = self._build_profile_context(user_profile)
        prompt = (
            f"为主题“{topic}”生成一份结构化的思维导图大纲，要求：\n"
            "1) 以 Markdown 列表格式输出；\n"
            "2) 包含中心主题和至少4个一级分支；\n"
            "3) 每个一级分支下包含2-4个二级子节点；\n"
            "4) 语言简洁，突出关键概念与关系；\n"
            "5) 只输出思维导图内容，不要额外说明。\n"
            f"学习者画像：{profile_context}"
        )
        text = self.client.generate_text(prompt)
        text, safe = self._safe_and_meta(text)
        res = LearningResource.objects.create(
            title=f"{topic} - 思维导图",
            resource_type='doc',
            content=text,
            author=self.user,
            metadata={'source': 'xinghuo', 'safe': safe.get('safe', True) if isinstance(safe, dict) else bool(safe), 'profile_used': user_profile is not None, 'mindmap': True},
        )
        return res

    def _build_profile_context(self, profile: dict) -> str:
        if not profile:
            return ""
        context = []
        if profile.get('cognitive_style') == '视觉型':
            context.append("视觉型学习者")
        return "；".join(context) if context else ""


class ReadingAgent(BaseAgent):
    """拓展阅读材料推荐智能体"""
    
    def generate_reading_list(self, topic: str, user_profile: dict = None) -> LearningResource:
        """生成拓展阅读材料列表"""
        profile_context = self._build_profile_context(user_profile)
        prompt = (
            f"为主题“{topic}”生成一份拓展阅读材料推荐列表，要求：\n"
            "1) 包含5-8篇推荐材料；\n"
            "2) 每篇包含：标题、来源/作者、核心内容简介、适合人群；\n"
            "3) 材料类型包括学术论文、技术博客、书籍章节等；\n"
            "4) 按难度和深度分级推荐；\n"
            "5) 以 Markdown 列表格式输出，不要额外说明。\n"
            f"学习者画像：{profile_context}"
        )
        text = self.client.generate_text(prompt)
        text, safe = self._safe_and_meta(text)
        res = LearningResource.objects.create(
            title=f"{topic} - 拓展阅读",
            resource_type='doc',
            content=text,
            author=self.user,
            metadata={'source': 'xinghuo', 'safe': safe.get('safe', True) if isinstance(safe, dict) else bool(safe), 'profile_used': user_profile is not None, 'reading_list': True},
        )
        return res

    def _build_profile_context(self, profile: dict) -> str:
        if not profile:
            return ""
        context = []
        if profile.get('learning_goals'):
            context.append(f"学习目标：{', '.join(profile['learning_goals'][:2])}")
        if profile.get('knowledge_profile'):
            context.append(f"知识基础：中级")
        return "；".join(context) if context else ""


def orchestrate_generate_resources(user, topic: str, resource_types=None, task: Optional[AgentTask] = None, user_profile: dict = None, outline_id: Optional[int] = None):
    """主协作函数：按请求的资源类型调用不同 agent，创建 LearningResource 并更新 AgentTask 输出。

    返回一个字典，包含每类资源的生成结果。
    """
    from .generation import GenerationManager

    if resource_types is None:
        resource_types = ['doc', 'ppt', 'quiz', 'code', 'mindmap', 'reading']

    # 优先使用传入的 outline_id，如果没有则从 task 中获取
    resolved_outline_id = outline_id
    if resolved_outline_id is None and task:
        # input_data 可能是 JSON 字符串，需要解析
        input_data = task.input_data
        if isinstance(input_data, str):
            try:
                import json
                input_data = json.loads(input_data)
            except Exception:
                input_data = {}
        if isinstance(input_data, dict):
            resolved_outline_id = input_data.get('outline_id')
    
    # 确保 outline_id 是整数
    if resolved_outline_id is not None:
        try:
            resolved_outline_id = int(resolved_outline_id)
        except (ValueError, TypeError):
            resolved_outline_id = None
    
    logger.info(f'orchestrate_generate_resources: topic={topic}, outline_id={resolved_outline_id}, task_id={task.id if task else None}')
    
    gm = GenerationManager(user, topic, outline_id=resolved_outline_id, 
                          task=task, resource_types=resource_types, user_profile=user_profile)
    return gm.generate()


class CriticAgent(BaseAgent):
    """审核员代理：审查生成内容并提供改进建议（基于COGENT框架的多维评估）"""
    
    CRITERIA = {
        'accuracy': '内容准确性',
        'completeness': '知识点完整性',
        'readability': '可读性',
        'alignment': '课程标准对齐度',
        'educational_value': '教育价值',
        'engagement': '学生参与度',
    }
    
    def review_content(self, topic: str, content: str, content_type: str = 'doc', 
                      standards: dict = None, readability_target: str = 'college') -> dict:
        """审查生成内容并返回改进建议
        
        Args:
            topic: 课程主题
            content: 待审查的内容
            content_type: 内容类型（doc/ppt/quiz）
            standards: 课程标准约束
            readability_target: 目标可读性水平
        
        Returns:
            审查报告，包含评分和改进建议
        """
        prompt = self._build_review_prompt(topic, content, content_type, standards, readability_target)
        text = self.client.generate_text(prompt, max_tokens=2048)
        text, _ = self._safe_and_meta(text)
        return self._parse_review_result(text)
    
    def _build_review_prompt(self, topic: str, content: str, content_type: str, 
                           standards: dict, readability_target: str) -> str:
        standards_text = json.dumps(standards or {}, ensure_ascii=False)
        
        return f"""
你是一位教育领域的专业审核员，需要审查以下课程内容。

【审查主题】{topic}
【内容类型】{content_type}
【目标可读性】{readability_target}
【课程标准】{standards_text}

【待审查内容】
{content[:3000]}

【审查要求】
请从以下维度进行评估，并提供具体改进建议：

1. 内容准确性：是否有事实错误或概念误解？
2. 知识点完整性：是否覆盖了核心知识点？是否有重要遗漏？
3. 可读性：语言难度是否适合目标受众？句子是否过长？
4. 课程标准对齐：是否符合相关课程标准要求？
5. 教育价值：是否能有效促进学习？是否有足够的示例和练习？
6. 学生参与度：是否有互动设计？是否能激发兴趣？

【输出格式】
请以JSON格式输出，包含以下字段：
- "score": 综合评分（0-100）
- "needs_revision": 是否需要修改（true/false）
- "criteria": 各维度评分{{"准确性": 80, "完整性": 75, ...}}
- "feedback": 具体改进建议列表
- "suggestions": 详细修改建议

请直接输出JSON，不要额外说明。
"""
    
    def _parse_review_result(self, text: str) -> dict:
        """解析审核结果"""
        result = {}  # 兜底：LLM 返回纯文本/占位（无花括号）时 re.search 命中不到，避免 UnboundLocalError
        try:
            result = json.loads(text)
        except Exception:
            # 尝试提取JSON部分
            match = re.search(r'\{[\s\S]*\}', text)
            if match:
                try:
                    result = json.loads(match.group(0))
                except Exception:
                    result = {}

        if not isinstance(result, dict):
            result = {}
        
        # 标准化输出
        return {
            'score': result.get('score', 70),
            'needs_revision': result.get('needs_revision', False),
            'criteria': result.get('criteria', {}),
            'feedback': result.get('feedback', []),
            'suggestions': result.get('suggestions', []),
        }
    
    def suggest_revision(self, topic: str, content: str, feedback: dict) -> str:
        """根据审核反馈生成修订建议"""
        feedback_text = json.dumps(feedback, ensure_ascii=False)
        
        prompt = f"""
你是一位教育内容优化专家，请根据以下审核反馈修订课程内容。

【主题】{topic}

【原始内容】
{content[:3000]}

【审核反馈】
{feedback_text}

【修订要求】
1. 根据反馈逐条修改内容
2. 保持原有的结构和格式
3. 确保修订后内容准确、完整、适合目标受众
4. 直接输出修订后的内容，不要额外说明

【输出】
修订后的完整内容
"""
        
        text = self.client.generate_text(prompt, max_tokens=3072)
        text, _ = self._safe_and_meta(text)
        return text

    def reconsider(self, topic: str, content: str, own_review: dict, other_review: dict) -> dict:
        """在与另一位审核员意见分歧时，重新评估一次（多智能体辩论第二轮）"""
        prompt = f"""
你之前对以下内容给出了审核意见：{json.dumps(own_review, ensure_ascii=False)}

另一位审核员（从不同角度）给出的意见是：{json.dumps(other_review, ensure_ascii=False)}

【主题】{topic}

【内容】
{content[:2000]}

请重新评估：如果认同对方的某些观点，调整你的评分和反馈；如果不认同，说明理由
并维持你的判断。

【输出格式】
请以JSON格式输出，包含以下字段：
- "score": 综合评分（0-100）
- "needs_revision": 是否需要修改（true/false）
- "criteria": 各维度评分
- "feedback": 具体改进建议列表
- "suggestions": 详细修改建议

请直接输出JSON，不要额外说明。
"""
        text = self.client.generate_text(prompt, max_tokens=1536)
        text, _ = self._safe_and_meta(text)
        return self._parse_review_result(text)


class DebateCriticAgent(CriticAgent):
    """第二视角审核员：聚焦事实准确性与逻辑严谨性，与 CriticAgent（教学设计视角）辩论
    （基于 Du et al., Improving Factuality and Reasoning in Language Models
    through Multiagent Debate, arXiv:2305.14325）"""

    def _build_review_prompt(self, topic: str, content: str, content_type: str,
                           standards: dict, readability_target: str) -> str:
        standards_text = json.dumps(standards or {}, ensure_ascii=False)

        return f"""
你是一位严格的事实核查与逻辑严谨性审核员。另一位审核员会从教学设计角度评审这份
内容，而你只关注：

1. 事实准确性：是否存在事实性错误、过时信息、数据或公式错误？
2. 逻辑严谨性：推导/论证是否有漏洞、跳跃或自相矛盾的地方？
3. 标准符合度：是否与给定课程标准冲突？

【审查主题】{topic}
【内容类型】{content_type}
【课程标准】{standards_text}

【待审查内容】
{content[:3000]}

【输出格式】
请以JSON格式输出，包含以下字段：
- "score": 综合评分（0-100）
- "needs_revision": 是否需要修改（true/false）
- "criteria": 各维度评分{{"事实准确性": 80, "逻辑严谨性": 75, "标准符合度": 85}}
- "feedback": 具体问题列表（每条尽量指出原文中的具体位置或说法）
- "suggestions": 修改建议

请直接输出JSON，不要额外说明。
"""


class StudentSimulatorAgent(BaseAgent):
    """学生模拟器代理：基于学生的6维画像扮演"虚拟学生"阅读内容，
    输出个性化适配诊断报告，并可针对诊断结果生成个性化改写内容。
    （参考 Generative Agents 的人格模拟思路，用于个性化内容适配）"""

    def _build_persona_description(self, profile: dict) -> str:
        if not profile:
            return "（暂无具体画像信息，请代入一名普通大学生）"

        parts = []
        kp = profile.get('knowledge_profile')
        if kp:
            parts.append(f"知识基础：{kp}")
        cs = profile.get('cognitive_style')
        if cs:
            parts.append(f"认知风格：{cs}")
        goals = profile.get('learning_goals') or []
        if goals:
            parts.append(f"学习目标：{'；'.join(str(g) for g in goals[:3])}")
        miscon = profile.get('misconceptions') or []
        if miscon:
            parts.append(f"已知易错点/常见误解：{'；'.join(str(m) for m in miscon[:3])}")
        prefs = profile.get('learning_preferences')
        if prefs:
            parts.append(f"学习偏好：{prefs}")

        return "\n".join(parts) if parts else "（暂无具体画像信息，请代入一名普通大学生）"

    def simulate_reading(self, topic: str, content: str, content_type: str, profile: dict) -> dict:
        """让模型扮演该学生阅读内容，返回个性化适配诊断报告"""
        persona = self._build_persona_description(profile)

        prompt = f"""
你将扮演一名正在学习"{topic}"的真实大学生，请完全代入下面这份学生画像的视角。

【学生画像】
{persona}

【正在阅读的学习材料】（类型：{content_type}）
{content[:3000]}

请以这名学生的第一人称视角"阅读"上面的材料，然后从这名学生（不是通用读者）的
角度评估：
1. comprehension_issues：由于这名学生的知识基础，材料中哪些地方他/她会看不懂、
   或者感觉跳跃太快（给出具体片段或知识点，最多3条）；
2. misconception_triggers：材料中哪些表述可能强化或没有纠正这名学生已有的易错
   认知（最多3条，若画像中没有易错点信息可留空）；
3. engagement_issues：哪些部分对这名学生来说太啰嗦、太简单或不感兴趣（最多3条）；
4. goal_alignment：0-100，材料对这名学生学习目标的契合程度；
5. overall_fit_score：0-100，综合来看这份材料对"这名学生"的适配程度；
6. suggestions：最多3条，如果要让这份材料更适合"这名学生"，应该如何调整
   （具体、可执行）；
7. persona_summary：用一句话总结这是怎样的一名学生。

只输出合法 JSON，不要任何额外说明，格式如下：
{{"persona_summary": "...", "overall_fit_score": 80, "goal_alignment": 80,
"comprehension_issues": ["..."], "misconception_triggers": ["..."],
"engagement_issues": ["..."], "suggestions": ["..."]}}
"""
        text = self.client.generate_text(prompt, max_tokens=1024)
        text, _ = self._safe_and_meta(text)
        return self._parse_simulation_result(text)

    def _parse_simulation_result(self, text: str) -> dict:
        result = None
        try:
            result = json.loads(text)
        except Exception:
            match = re.search(r'\{[\s\S]*\}', text)
            if match:
                try:
                    result = json.loads(match.group(0))
                except Exception:
                    result = None

        if not isinstance(result, dict):
            result = {}

        return {
            'persona_summary': result.get('persona_summary', ''),
            'overall_fit_score': result.get('overall_fit_score', 70),
            'goal_alignment': result.get('goal_alignment', 70),
            'comprehension_issues': result.get('comprehension_issues', []) or [],
            'misconception_triggers': result.get('misconception_triggers', []) or [],
            'engagement_issues': result.get('engagement_issues', []) or [],
            'suggestions': result.get('suggestions', []) or [],
        }

    def personalize_revision(self, topic: str, content: str, content_type: str, simulation_report: dict) -> str:
        """根据模拟学生反馈，对内容做一次针对性的个性化改写"""
        report_text = json.dumps(simulation_report, ensure_ascii=False)

        prompt = f"""
你是一位教学内容个性化改写专家。下面是一份"模拟学生"对学习材料的真实反馈，请据此
改写材料，使其更适合这名学生，但不能改变材料的整体结构、类型和长度量级。

【主题】{topic}

【原材料】（类型：{content_type}）
{content[:3000]}

【模拟学生反馈】
{report_text}

【改写要求】
1. 针对 comprehension_issues 中提到的看不懂的地方，补充更基础的解释或过渡；
2. 针对 misconception_triggers，加入纠正性说明，明确指出常见误区；
3. 针对 engagement_issues，删减/精简该学生觉得啰嗦或无意义重复的部分；
4. 保持原有格式（如 Markdown 标题/列表结构），直接输出改写后的完整内容，
   不要输出任何额外说明。
"""
        text = self.client.generate_text(prompt, max_tokens=3072)
        text, _ = self._safe_and_meta(text)
        return text


class PeerLearnerAgent(BaseAgent):
    """同伴学习者代理："小艾"——一名学习进度比当前学生稍慢的同伴。
    通过"费曼学习法"（向同伴讲解以巩固理解）主动请学生讲解某个主题、
    针对讲解提出追问，并在多轮对话后评估讲解质量，用于更新学生画像
    （cognitive_style/misconceptions）。"""

    def _build_peer_persona_description(self, profile: dict) -> str:
        if not profile:
            return "（请代入一名刚接触这个主题、基础比对方稍弱的同学）"

        parts = []
        kp = profile.get('knowledge_profile')
        if kp:
            parts.append(f"对方的知识基础大致是：{kp}（你比对方稍弱一些，对这个主题还不太熟）")
        goals = profile.get('learning_goals') or []
        if goals:
            parts.append(f"对方的学习目标：{'；'.join(str(g) for g in goals[:3])}")

        return "\n".join(parts) if parts else "（请代入一名刚接触这个主题、基础比对方稍弱的同学）"

    def respond(self, topic: str, conversation_history: list, profile: dict, memories: list = None) -> str:
        """让模型扮演"小艾"，向学生请教某个主题或针对学生的讲解追问"""
        persona = self._build_peer_persona_description(profile)
        history_text = "\n".join(conversation_history[-10:]) if conversation_history else "（还没有开始对话）"

        if conversation_history:
            task_desc = (
                "如果对方已经讲解过，针对讲解中最可能被忽略的边界条件、前提或易混淆点，"
                "提出一个具体的追问（不要泛泛地说'还有吗'）；"
                "如果对方的讲解已经足够清楚，就给出一句具体的'原来是这样！'式认可，"
                "再追加一个稍微深入一点的延伸问题。"
            )
        else:
            task_desc = f"这是对话的第一轮，请用一两句话请对方用通俗的话讲讲\"{topic}\"是什么。"

        memory_text = ""
        if memories:
            memory_lines = "\n".join(f"- {m.get('content', '')}" for m in memories if isinstance(m, dict) and m.get('content'))
            if memory_lines:
                memory_text = f"""
【你的记忆】（之前和这位同学交流时留下的印象，可在合适时机自然提起）
{memory_lines}
"""

        prompt = f"""
你扮演一个名叫"小艾"的同学，学习进度比对方稍慢，正在向对方请教"{topic}"。

【你的人设】
{persona}
{memory_text}
【对话历史】（Student是对方，小艾是你）
{history_text}

【任务】
{task_desc}

【输出要求】
只输出"小艾"说的话本身，不要任何旁白、引号、JSON或"小艾："这样的前缀。
"""

        text = self.client.generate_text(prompt, max_tokens=512)
        text, _ = self._safe_and_meta(text)
        return text

    def evaluate_session(self, topic: str, conversation_text: str, profile: dict) -> dict:
        """评估一段"学生讲解+小艾追问"的对话，输出对学生讲解的诊断"""
        prompt = f"""
下面是一段关于"{topic}"的对话：一名学生在向同伴"小艾"讲解这个主题，小艾不断追问。

【对话内容】
{conversation_text[:3000]}

请以教学诊断专家的视角评估这名学生的讲解，输出JSON：
1. understood_points：学生讲解中清晰、准确的知识点（最多3条）；
2. gap_points：学生讲解中遗漏或讲得不够清楚的地方（最多3条）；
3. misconceptions_detected：学生讲解中暴露出的错误认知（最多3条，没有则留空数组）；
4. cognitive_style_observation：用一句中文简短描述这名学生的讲解风格
   （例如"倾向于用类比和生活实例解释抽象概念"），没有明显特征则留空字符串；
5. teaching_score：0-100，综合评价这次讲解的清晰度和准确度；
6. memory_observations：值得"小艾"记住、供下次交流参考的观察（最多3条），
   每条格式为 {{"content": "一句话描述", "importance": 1-10的整数}}，
   importance越大表示这条观察越值得长期记住（例如反复出现的误区可以打8-9分，
   一次性的小细节打2-3分），没有则留空数组。

只输出合法JSON，不要任何额外说明，格式如下：
{{"understood_points": ["..."], "gap_points": ["..."], "misconceptions_detected": ["..."],
"cognitive_style_observation": "...", "teaching_score": 80,
"memory_observations": [{{"content": "...", "importance": 7}}]}}
"""

        text = self.client.generate_text(prompt, max_tokens=512)
        text, _ = self._safe_and_meta(text)
        return self._parse_evaluation_result(text)

    def _parse_evaluation_result(self, text: str) -> dict:
        result = None
        try:
            result = json.loads(text)
        except Exception:
            match = re.search(r'\{[\s\S]*\}', text)
            if match:
                try:
                    result = json.loads(match.group(0))
                except Exception:
                    result = None

        if not isinstance(result, dict):
            result = {}

        memory_observations = []
        for item in result.get('memory_observations', []) or []:
            if not isinstance(item, dict):
                continue
            content = str(item.get('content') or '').strip()
            if not content:
                continue
            try:
                importance = int(item.get('importance', 5))
            except (TypeError, ValueError):
                importance = 5
            importance = max(1, min(10, importance))
            memory_observations.append({'content': content, 'importance': importance})

        return {
            'understood_points': result.get('understood_points', []) or [],
            'gap_points': result.get('gap_points', []) or [],
            'misconceptions_detected': result.get('misconceptions_detected', []) or [],
            'cognitive_style_observation': result.get('cognitive_style_observation', ''),
            'teaching_score': result.get('teaching_score', 70),
            'memory_observations': memory_observations,
        }

    def select_relevant_memories(self, memory_stream: list, topic: str, k: int = 3) -> list:
        """从记忆流中检索与当前主题最相关的k条记忆。

        参考Generative Agents（Park et al. 2023）的检索机制：
        retrieval_score = relevance + recency + importance，三者各占1/3权重，
        分数最高的k条记忆将被"小艾"在对话中参考。
        """
        if not memory_stream:
            return []

        now = timezone.now()
        topic_chars = set(str(topic or ''))
        scored = []
        for mem in memory_stream:
            if not isinstance(mem, dict):
                continue
            content = str(mem.get('content') or '')
            mem_topic = str(mem.get('topic') or '')
            mem_chars = set(mem_topic) | set(content)
            relevance = len(topic_chars & mem_chars) / max(len(topic_chars), 1)

            timestamp = parse_datetime(mem.get('last_accessed_at') or mem.get('created_at') or '')
            if timestamp:
                hours_ago = max((now - timestamp).total_seconds() / 3600.0, 0)
            else:
                hours_ago = 24 * 365  # 缺失时间戳视为很久之前
            recency = math.exp(-hours_ago / 168.0)  # 一周衰减

            try:
                importance = float(mem.get('importance', 5)) / 10.0
            except (TypeError, ValueError):
                importance = 0.5

            score = (relevance + recency + importance) / 3.0
            scored.append((score, mem))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [mem for _, mem in scored[:k]]

    def generate_reflection(self, memory_stream: list, topic: str, threshold: int = 15) -> Optional[dict]:
        """参考Generative Agents的反思（reflection）机制：

        当上一次反思之后新增观察记忆的重要度之和达到阈值时，让"小艾"综合这些
        观察，生成一条更高层的反思，写回记忆流供下次检索参考。未达到阈值或
        没有可用观察时返回 None。
        """
        observations = [m for m in (memory_stream or []) if isinstance(m, dict) and m.get('type') != 'reflection']
        if not observations:
            return None

        reflections = [m for m in memory_stream if isinstance(m, dict) and m.get('type') == 'reflection']
        last_reflection_at = reflections[-1].get('created_at') if reflections else ''

        recent = [m for m in observations if str(m.get('created_at', '')) > str(last_reflection_at)]
        if not recent:
            return None

        total_importance = 0.0
        for m in recent:
            try:
                total_importance += float(m.get('importance', 0))
            except (TypeError, ValueError):
                continue
        if total_importance < threshold:
            return None

        contents = "\n".join(f"- {m.get('content', '')}" for m in recent[-10:])
        prompt = f"""
以下是"小艾"在和同一名学生多次互教交流中积累的观察记录（关于主题"{topic}"）：
{contents}

请综合这些观察，用一两句中文总结这名学生在学习这类主题时反复出现的整体特点或
模式（例如"经常把XX和YY搞混"、"擅长用类比但容易忽略边界条件"等），作为"小艾"
对这名同学的整体印象，供下次交流时参考。

只输出这一两句话本身，不要任何前缀、引号或说明。
"""

        text = self.client.generate_text(prompt, max_tokens=200)
        text, _ = self._safe_and_meta(text)
        text = text.strip()
        if not text:
            return None

        return {'content': text, 'importance': 8, 'topic': topic, 'type': 'reflection'}


class SelfExplanationAgent(BaseAgent):
    """自我解释评估代理（Self-Explanation Effect, Chi et al.）。

    在导师给出讲解后，引导学生用自己的话复述/解释这段内容，再评估这段
    自我解释是否完整、准确，发现遗漏或误解后反馈给学生并写入知识画像。"""

    def evaluate(self, topic: str, explanation_given: str, student_explanation: str) -> dict:
        prompt = f"""
下面是导师刚才关于"{topic}"给学生的一段讲解，以及学生随后尝试用自己的话做的
自我解释。

【导师的讲解】
{explanation_given[:1500]}

【学生的自我解释】
{student_explanation[:1000]}

请以教学诊断专家的视角评估这段自我解释，输出JSON：
1. concept：这段讲解和自我解释所围绕的核心知识点名称（一个简短的中文短语，
   例如"梯度下降"、"矩阵乘法"），如果难以判断就给一个最贴近的主题词；
2. covered_points：学生自我解释中准确覆盖到的要点（最多3条）；
3. gaps：学生自我解释中遗漏的要点（最多3条，没有则留空数组）；
4. misconceptions：学生自我解释中暴露出的错误理解（最多3条，没有则留空数组）；
5. quality_score：0-100，综合评价这段自我解释的完整性和准确度；
6. feedback：一两句中文反馈，先肯定学生讲对的地方，再指出需要纠正或补充的地方。

只输出合法JSON，不要任何额外说明，格式如下：
{{"concept": "...", "covered_points": ["..."], "gaps": ["..."],
"misconceptions": ["..."], "quality_score": 80, "feedback": "..."}}
"""
        text = self.client.generate_text(prompt, max_tokens=512)
        text, _ = self._safe_and_meta(text)
        return self._parse_result(text)

    def _parse_result(self, text: str) -> dict:
        result = None
        try:
            result = json.loads(text)
        except Exception:
            match = re.search(r'\{[\s\S]*\}', text)
            if match:
                try:
                    result = json.loads(match.group(0))
                except Exception:
                    result = None

        if not isinstance(result, dict):
            result = {}

        try:
            quality_score = float(result.get('quality_score', 70))
        except (TypeError, ValueError):
            quality_score = 70.0
        quality_score = max(0.0, min(100.0, quality_score))

        return {
            'concept': str(result.get('concept') or '').strip(),
            'covered_points': result.get('covered_points', []) or [],
            'gaps': result.get('gaps', []) or [],
            'misconceptions': result.get('misconceptions', []) or [],
            'quality_score': quality_score,
            'feedback': str(result.get('feedback') or '').strip(),
        }


class ReflectionController:
    """反思控制器：管理反思-改进循环（基于Instructional Agents论文）"""
    
    def __init__(self, user, max_iterations: int = 3, quality_threshold: int = 85):
        self.user = user
        self.max_iterations = max_iterations
        self.quality_threshold = quality_threshold
        self.critic = CriticAgent(user)
        self.debate_critic = DebateCriticAgent(user, client=self.critic.client)
        self.client = self.critic.client  # plan_review 需要直接调用 client

    def debate_review(self, topic: str, content: str, content_type: str,
                       standards: dict, readability_target: str) -> dict:
        """多智能体辩论式审核：两位审核员独立评审，分歧较大时再各自重新评估一次
        （基于 Du et al., arXiv:2305.14325 的多智能体辩论思路）"""
        review_a = self.critic.review_content(topic, content, content_type, standards, readability_target)
        review_b = self.debate_critic.review_content(topic, content, content_type, standards, readability_target)

        rounds = [
            {'critic': 'CriticAgent', 'score': review_a['score'],
             'needs_revision': review_a['needs_revision'], 'feedback': review_a['feedback']},
            {'critic': 'DebateCriticAgent', 'score': review_b['score'],
             'needs_revision': review_b['needs_revision'], 'feedback': review_b['feedback']},
        ]

        disagree = abs(review_a['score'] - review_b['score']) >= 15 or review_a['needs_revision'] != review_b['needs_revision']
        if disagree:
            review_a = self.critic.reconsider(topic, content, review_a, review_b)
            review_b = self.debate_critic.reconsider(topic, content, review_b, review_a)
            rounds.append({'critic': 'CriticAgent', 'score': review_a['score'],
                            'needs_revision': review_a['needs_revision'], 'feedback': review_a['feedback']})
            rounds.append({'critic': 'DebateCriticAgent', 'score': review_b['score'],
                            'needs_revision': review_b['needs_revision'], 'feedback': review_b['feedback']})

        return {
            'score': round((review_a['score'] + review_b['score']) / 2),
            'needs_revision': bool(review_a['needs_revision'] or review_b['needs_revision']),
            'criteria': review_a.get('criteria', {}),
            'feedback': (review_a.get('feedback') or []) + (review_b.get('feedback') or []),
            'suggestions': (review_a.get('suggestions') or []) + (review_b.get('suggestions') or []),
            'debate_rounds': rounds,
        }

    def iterative_improvement(self, topic: str, content: str, content_type: str = 'doc',
                             standards: dict = None, readability_target: str = 'college') -> dict:
        """执行反思-改进循环
        
        Args:
            topic: 课程主题
            content: 初始内容
            content_type: 内容类型
            standards: 课程标准
            readability_target: 目标可读性水平
        
        Returns:
            最终结果，包含最终内容和迭代历史
        """
        iterations = []
        current_content = content
        final_score = 0
        
        logger.info(f'=== 开始反思改进循环 === topic={topic}, content_type={content_type}')
        
        for iteration in range(self.max_iterations):
            logger.info(f'--- 迭代 {iteration + 1}/{self.max_iterations} ---')
            
            # 第1步：审核当前内容（第1轮采用多智能体辩论审核，后续轮次单一审核员）
            logger.info('开始审核内容...')
            if iteration == 0:
                review = self.debate_review(
                    topic, current_content, content_type, standards, readability_target
                )
            else:
                review = self.critic.review_content(
                    topic, current_content, content_type, standards, readability_target
                )
                review['debate_rounds'] = []
            final_score = review['score']
            logger.info(f'审核完成，评分: {final_score}, 需要改进: {review["needs_revision"]}')

            iterations.append({
                'iteration': iteration + 1,
                'score': review['score'],
                'needs_revision': review['needs_revision'],
                'feedback': review['feedback'],
                'debate_rounds': review.get('debate_rounds', []),
            })
            
            # 检查是否达到质量要求
            if review['score'] >= self.quality_threshold:
                logger.info(f'达到质量要求，退出循环')
                break
            
            # 第2步：根据反馈修订内容
            if review['needs_revision'] and review.get('feedback'):
                logger.info('开始修订内容...')
                current_content = self.critic.suggest_revision(topic, current_content, review)
                logger.info('修订完成')
            else:
                logger.info('不需要修订，退出循环')
                break
        
        logger.info(f'=== 反思改进循环结束 === 最终评分: {final_score}, 迭代次数: {len(iterations)}')
        
        return {
            'final_content': current_content,
            'final_score': final_score,
            'iterations': iterations,
            'total_iterations': len(iterations),
            'quality_met': final_score >= self.quality_threshold,
        }
    
    def plan_review(self, topic: str, outline: dict) -> dict:
        """规划阶段审核：检查课程大纲是否符合要求"""
        outline_text = json.dumps(outline, ensure_ascii=False)
        
        prompt = f"""
你是一位课程规划专家，请审查以下课程大纲。

【主题】{topic}

【课程大纲】
{outline_text}

【审查维度】
1. 结构完整性：章节划分是否合理？是否有逻辑漏洞？
2. 目标明确性：学习目标是否清晰可衡量？
3. 内容覆盖：是否覆盖核心知识点？
4. 难度递进：是否符合循序渐进原则？
5. 评估设计：是否有适当的练习和评估环节？

【输出格式】
JSON格式：{{"score": 85, "feedback": ["改进点1", "改进点2"], "suggestions": ["建议1"]}}
"""
        
        text = self.client.generate_text(prompt)
        try:
            result = json.loads(text)
        except Exception:
            try:
                match = re.search(r'\{[\s\S]*\}', text)
                result = json.loads(match.group(0)) if match else {}
            except Exception:
                result = {}

        return result