"""智能辅导模块

提供即时、多模态的答疑解惑服务，支持：
1. 文字解答
2. 图解说明
3. 代码解释
4. 短视频讲解脚本生成
"""
import logging
from typing import Optional, Dict, List
from django.utils import timezone

from .xinghuo_client import XinghuoClient
from .safety import check_text, censor_text, check_with_xinghuo
from agent_system.models import Conversation, Message, StudentProfile

logger = logging.getLogger(__name__)


class TutorAgent:
    """智能辅导智能体"""
    
    def __init__(self, user, conversation_id: Optional[int] = None):
        self.user = user
        self.client = XinghuoClient()
        self.conversation_id = conversation_id
        self.conversation = self._get_or_create_conversation()
        self.max_history = 10
    
    def _get_or_create_conversation(self) -> Conversation:
        """获取或创建对话会话"""
        if self.conversation_id:
            try:
                return Conversation.objects.get(pk=self.conversation_id, user=self.user)
            except Conversation.DoesNotExist:
                pass
        
        # 创建新会话
        return Conversation.objects.create(user=self.user, title='智能辅导对话')
    
    def _get_profile_context(self) -> str:
        """获取用户画像上下文"""
        try:
            profile = self.user.student_profile
            context = []
            if profile.knowledge_profile:
                context.append(f"知识基础：{profile.knowledge_profile}")
            if profile.cognitive_style:
                context.append(f"认知风格：{profile.cognitive_style}")
            if profile.misconceptions:
                # 易错点条目可能是 dict 或字符串，统一取文本，避免 join 时 TypeError
                texts = []
                for m in profile.misconceptions[:3]:
                    t = (m.get('concept') or m.get('text') or m.get('description') or m.get('label')) if isinstance(m, dict) else m
                    t = str(t or '').strip()
                    if t:
                        texts.append(t)
                if texts:
                    context.append(f"易错点：{', '.join(texts)}")
            if profile.learning_preferences:
                prefs = profile.learning_preferences or {}
                # 偏好键在不同写入方各不相同，逐一兜底（preferred_format 其实从没被写过）
                fmt = prefs.get('preferred_format') or prefs.get('content_formats') or prefs.get('preferred_mode') or prefs.get('difficulty_preference')
                if fmt:
                    if isinstance(fmt, (list, tuple)):
                        fmt = '、'.join(str(x) for x in fmt if x)
                    if str(fmt).strip():
                        context.append(f"偏好：{fmt}")
            return "；".join(context)
        except StudentProfile.DoesNotExist:
            return ""
    
    def _get_conversation_history(self) -> List[Dict]:
        """获取对话历史（最近N条）"""
        messages = Message.objects.filter(
            conversation=self.conversation
        ).order_by('created_at')[-self.max_history:]
        
        history = []
        for msg in messages:
            history.append({
                'role': 'user' if msg.role == 'student' else 'assistant',
                'content': msg.content,
            })
        return history
    
    def _save_message(self, content: str, role: str = 'student', content_type: str = 'text', metadata: Optional[dict] = None):
        """保存消息"""
        Message.objects.create(
            conversation=self.conversation,
            role=role,
            content=content,
            content_type=content_type,
            metadata=metadata or {}
        )
    
    def _safe_response(self, text: str) -> str:
        """安全检查响应内容"""
        try:
            meta = check_with_xinghuo(text)
        except Exception:
            meta = check_text(text)
        
        if isinstance(meta, dict) and not meta.get('safe', True):
            logger.warning('Tutor response contains sensitive content')
            return censor_text(text)
        return text
    
    def answer_question(self, question: str, mode: str = 'text') -> Dict:
        """回答问题，支持多种模式"""
        # 保存用户问题
        self._save_message(question, role='student', content_type='text')
        
        # 获取上下文
        profile_context = self._get_profile_context()
        history = self._get_conversation_history()
        
        # 构建提示词
        mode_instructions = {
            'text': '请详细解答这个问题，用清晰的文字说明。',
            'visual': '请用结构化的方式解释这个问题，包含图表说明（用文本描述）。',
            'code': '如果涉及代码，请提供代码示例和详细解释。',
            'video': '请生成一个短视频脚本来解释这个问题（分镜头格式）。',
            'step_by_step': '请按步骤详细解释，一步步引导理解。',
        }
        
        instruction = mode_instructions.get(mode, mode_instructions['text'])
        
        messages = [
            {
                'role': 'system',
                'content': f"""
你是一名专业的AI辅导老师，擅长解答高等教育相关的问题。
请遵循以下原则：
1. 回答要准确、清晰、易懂
2. 根据用户画像提供个性化解答
3. 遇到不确定的问题要诚实说明
4. 使用中文回答

学习者画像：{profile_context}
""".strip()
            }
        ]
        
        # 添加历史对话
        messages.extend(history)
        
        # 添加当前问题
        messages.append({
            'role': 'user',
            'content': f"{instruction}\n\n问题：{question}"
        })
        
        # 调用大模型
        try:
            if hasattr(self.client, 'get_response'):
                response = self.client.get_response(messages)
            else:
                # 构建合并的prompt
                prompt = "\n".join([f"{m['role']}: {m['content']}" for m in messages])
                response = self.client.generate_text(prompt, max_tokens=2048)
            
            # 安全检查
            safe_response = self._safe_response(response)
            
            # 保存助手回复
            self._save_message(safe_response, role='assistant', content_type=mode)
            
            return {
                'success': True,
                'answer': safe_response,
                'mode': mode,
                'conversation_id': self.conversation.id,
                'timestamp': timezone.now().isoformat(),
            }
        
        except Exception as e:
            logger.exception('Tutor answer failed')
            return {
                'success': False,
                'error': str(e),
                'conversation_id': self.conversation.id,
            }
    
    def explain_concept(self, concept: str, depth: str = 'medium') -> Dict:
        """解释概念"""
        depth_instructions = {
            'basic': '用简单易懂的方式解释这个概念，适合初学者。',
            'medium': '中等深度解释，包含基本原理和示例。',
            'advanced': '深入解释，包含数学推导和高级应用。',
        }
        
        question = f"请解释概念：{concept}"
        instruction = depth_instructions.get(depth, depth_instructions['medium'])
        
        return self.answer_question(f"{instruction}\n\n{question}", mode='text')
    
    def solve_problem(self, problem: str, subject: str = '') -> Dict:
        """解答题目/问题"""
        context = f"科目：{subject}\n" if subject else ""
        question = f"{context}请解答以下问题：\n{problem}"
        return self.answer_question(question, mode='step_by_step')
    
    def explain_code(self, code: str, language: str = 'python') -> Dict:
        """解释代码"""
        question = f"请解释以下{language}代码的功能和执行流程：\n```\n{code}\n```"
        return self.answer_question(question, mode='code')
    
    def generate_example(self, topic: str, type: str = 'code') -> Dict:
        """生成示例"""
        if type == 'code':
            question = f"请为主题'{topic}'生成一个完整的可运行代码示例，并包含详细注释。"
            return self.answer_question(question, mode='code')
        elif type == 'visual':
            question = f"请为主题'{topic}'生成一个图解说明（用文本描述图表结构）。"
            return self.answer_question(question, mode='visual')
        else:
            question = f"请为主题'{topic}'生成一个详细示例。"
            return self.answer_question(question, mode='text')
    
    def get_conversation(self) -> Dict:
        """获取完整对话"""
        messages = Message.objects.filter(conversation=self.conversation).order_by('created_at')
        
        return {
            'conversation_id': self.conversation.id,
            'title': self.conversation.title,
            'created_at': self.conversation.created_at.isoformat(),
            'updated_at': self.conversation.updated_at.isoformat(),
            'messages': [
                {
                    'id': msg.id,
                    'role': msg.role,
                    'content': msg.content,
                    'content_type': msg.content_type,
                    'created_at': msg.created_at.isoformat(),
                }
                for msg in messages
            ],
        }
    
    def summarize_conversation(self) -> str:
        """总结对话内容"""
        history = self._get_conversation_history()
        if not history:
            return "对话为空"
        
        messages_text = "\n".join([f"{m['role']}: {m['content'][:200]}" for m in history])
        
        prompt = f"""请总结以下对话内容：

{messages_text}

请用简洁的语言概括对话的主要内容和结论。
"""
        
        try:
            summary = self.client.generate_text(prompt)
            # 更新对话摘要
            self.conversation.context_summary = summary
            self.conversation.save()
            return summary
        except Exception as e:
            logger.exception('Summarization failed')
            return "无法生成摘要"


class SocraticTutor(TutorAgent):
    """苏格拉底式辅导智能体
    
    通过提问引导学生思考，而不是直接给出答案。
    """
    
    def ask_question(self, topic: str, current_question: Optional[str] = None) -> Dict:
        """提出引导性问题"""
        profile_context = self._get_profile_context()
        
        if current_question:
            prompt = f"""
你是一位苏格拉底式的老师。不要直接回答问题，而是通过提问引导学生自己思考。

学习者画像：{profile_context}

当前问题：{current_question}

请提出一个引导性问题，帮助学生深入思考，而不是直接给出答案。
"""
        else:
            prompt = f"""
你是一位苏格拉底式的老师。请围绕主题提出启发性问题，引导学生思考。

学习者画像：{profile_context}

主题：{topic}

请提出一个引导性问题，帮助学生深入理解这个主题。
"""
        
        try:
            response = self.client.generate_text(prompt)
            safe_response = self._safe_response(response)
            
            self._save_message(safe_response, role='assistant', content_type='text')
            
            return {
                'success': True,
                'question': safe_response,
                'conversation_id': self.conversation.id,
            }
        except Exception as e:
            logger.exception('Socratic question failed')
            return {
                'success': False,
                'error': str(e),
            }
    
    def guide_discussion(self, topic: str, student_response: str) -> Dict:
        """根据学生回答继续引导讨论"""
        profile_context = self._get_profile_context()
        
        prompt = f"""
你是一位苏格拉底式的老师。请根据学生的回答，提出下一个引导性问题，帮助学生深入思考。

学习者画像：{profile_context}

主题：{topic}

学生的回答：{student_response}

请提出一个引导性问题，继续引导学生思考，不要直接给出答案。
"""
        
        try:
            response = self.client.generate_text(prompt)
            safe_response = self._safe_response(response)
            
            self._save_message(student_response, role='student', content_type='text')
            self._save_message(safe_response, role='assistant', content_type='text')
            
            return {
                'success': True,
                'question': safe_response,
                'conversation_id': self.conversation.id,
            }
        except Exception as e:
            logger.exception('Socratic guide failed')
            return {
                'success': False,
                'error': str(e),
            }