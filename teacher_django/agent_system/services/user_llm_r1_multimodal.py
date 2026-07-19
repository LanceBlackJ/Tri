"""
USER-LLM R1 完整实现：多模态处理服务
支持图片、语音等多模态输入
"""
import json
import logging
from typing import Dict, List, Optional
from datetime import datetime

from django.contrib.auth import get_user_model

from .xinghuo_client import XinghuoClient

logger = logging.getLogger(__name__)
User = get_user_model()


class MultimodalProcessor:
    """多模态处理服务"""
    
    def __init__(self):
        self.client = XinghuoClient()
        self.__supports_multimodal = hasattr(self.client, 'generate_multimodal_content')
    
    def process_text_input(self, text: str) -> Dict:
        """处理纯文本输入"""
        return {
            'modality': 'text',
            'content': text,
            'processed_at': datetime.now().isoformat(),
            'features': self._extract_text_features(text)
        }
    
    def process_image_input(
        self,
        image_data: str,
        context: str = ""
    ) -> Dict:
        """
        处理图片输入
        
        Args:
            image_data: base64编码的图片数据或图片URL
            context: 可选的上下文描述
            
        Returns:
            图片分析结果
        """
        try:
            # 如果是base64编码
            if image_data.startswith('data:image'):
                # 提取base64数据
                image_data = image_data.split(',')[1]
            
            # 调用VLM分析图片
            if self.__supports_multimodal:
                result = self._analyze_with_vlm(image_data, context)
            else:
                # 降级处理：返回占位结果
                result = self._fallback_image_analysis(context)
            
            return {
                'modality': 'image',
                'image_description': result.get('description', ''),
                'extracted_info': result.get('extracted_info', {}),
                'learning_indicators': result.get('learning_indicators', []),
                'processed_at': datetime.now().isoformat()
            }
        except Exception as e:
            logger.error(f"Image processing failed: {e}")
            return {
                'modality': 'image',
                'error': str(e),
                'processed_at': datetime.now().isoformat()
            }
    
    def process_multimodal_input(
        self,
        text: str = "",
        image: Optional[str] = None,
        context: Dict = None
    ) -> Dict:
        """
        处理多模态输入
        
        Args:
            text: 文本输入
            image: 图片输入（base64或URL）
            context: 额外上下文信息
        """
        context = context or {}
        results = {
            'text': None,
            'image': None,
            'combined_analysis': None,
            'processed_at': datetime.now().isoformat()
        }
        
        # 处理文本
        if text:
            results['text'] = self.process_text_input(text)
        
        # 处理图片
        if image:
            results['image'] = self.process_image_input(image, context_text_from_context(context))
        
        # 合并分析
        if results['text'] and results['image']:
            results['combined_analysis'] = self._combine_analyses(
                results['text'],
                results['image'],
                context
            )
        elif results['text']:
            results['combined_analysis'] = results['text']
        elif results['image']:
            results['combined_analysis'] = results['image']
        
        return results
    
    def _extract_text_features(self, text: str) -> Dict:
        """提取文本特征"""
        features = {
            'length': len(text),
            'has_question': '？' in text or '?' in text,
            'has_goal_keywords': self._has_learning_goal_keywords(text),
            'has_preference_keywords': self._has_preference_keywords(text),
            'sentiment': self._estimate_sentiment(text)
        }
        return features
    
    def _has_learning_goal_keywords(self, text: str) -> bool:
        """检查是否包含学习目标关键词"""
        keywords = ['考试', '考研', '项目', '入门', '掌握', '学习', '目标', '计划']
        return any(kw in text for kw in keywords)
    
    def _has_preference_keywords(self, text: str) -> bool:
        """检查是否包含学习偏好关键词"""
        keywords = ['视频', '文字', '练习', '图', '听', '讲解', '实践', '喜欢', '希望']
        return any(kw in text for kw in keywords)
    
    def _estimate_sentiment(self, text: str) -> str:
        """估计情感倾向"""
        positive = ['好', '棒', '不错', '懂了', '谢谢', '明白']
        negative = ['不懂', '不会', '困惑', '难', '迷茫']
        
        pos_count = sum(1 for w in positive if w in text)
        neg_count = sum(1 for w in negative if w in text)
        
        if pos_count > neg_count:
            return 'positive'
        elif neg_count > pos_count:
            return 'negative'
        return 'neutral'
    
    def _analyze_with_vlm(self, image_data: str, context: str) -> Dict:
        """使用VLM分析图片"""
        prompt = f"""分析这张图片，提取以下信息用于构建用户学习画像：

1. 图片内容描述（简明扼要）
2. 提取的学习相关信息：
   - 学习材料类型（文档、笔记、PPT、代码等）
   - 涉及的学科或知识点
   - 学习环境（桌面、移动设备等）
   - 可能的用户状态（专注、分心等）
3. 学习指标：
   - 是否有手写笔记
   - 是否有图表或公式
   - 内容的复杂度
   - 是否有学习进度标记

请以JSON格式返回：
{{
    "description": "图片内容描述",
    "extracted_info": {{
        "material_type": "类型",
        "subject": "学科",
        "environment": "环境",
        "user_state": "用户状态"
    }},
    "learning_indicators": [
        {{"indicator": "指标名称", "value": "指标值", "confidence": 0.0-1.0}}
    ]
}}

图片上下文: {context if context else '无'}"""

        try:
            # 调用支持多模态的API
            response = self.client.generate_multimodal_content(
                prompt=prompt,
                image_data=image_data
            )
            return json.loads(response)
        except Exception as e:
            logger.error(f"VLM analysis failed: {e}")
            return self._fallback_image_analysis(context)
    
    def _fallback_image_analysis(self, context: str) -> Dict:
        """降级的图片分析（当VLM不可用时）"""
        return {
            'description': f'用户上传了图片（VLM分析不可用）',
            'extracted_info': {
                'material_type': 'unknown',
                'subject': 'unknown',
                'environment': 'unknown',
                'user_state': 'unknown'
            },
            'learning_indicators': []
        }
    
    def _combine_analyses(
        self,
        text_result: Dict,
        image_result: Dict,
        context: Dict
    ) -> Dict:
        """合并文本和图片的分析结果"""
        combined = {
            'user_intent': self._infer_user_intent(text_result, image_result),
            'learning_context': self._extract_learning_context(text_result, image_result),
            'profile_hints': self._extract_profile_hints(text_result, image_result),
            'confidence_weight': self._calculate_confidence_weight(text_result, image_result)
        }
        return combined
    
    def _infer_user_intent(
        self,
        text_result: Dict,
        image_result: Dict
    ) -> Dict:
        """推断用户意图"""
        intents = []
        
        # 从文本推断
        if text_result.get('features', {}).get('has_question'):
            intents.append('question')
        if text_result.get('features', {}).get('has_goal_keywords'):
            intents.append('goal_expression')
        if text_result.get('features', {}).get('has_preference_keywords'):
            intents.append('preference_expression')
        
        # 从图片推断
        if image_result.get('extracted_info', {}).get('material_type'):
            intents.append('material_sharing')
        
        return {
            'primary_intent': intents[0] if intents else 'general',
            'all_intents': intents,
            'confidence': min(1.0, len(intents) * 0.3)
        }
    
    def _extract_learning_context(
        self,
        text_result: Dict,
        image_result: Dict
    ) -> Dict:
        """提取学习上下文"""
        context = {
            'subject': image_result.get('extracted_info', {}).get('subject', ''),
            'material_type': image_result.get('extracted_info', {}).get('material_type', ''),
            'environment': image_result.get('extracted_info', {}).get('environment', '')
        }
        return context
    
    def _extract_profile_hints(
        self,
        text_result: Dict,
        image_result: Dict
    ) -> List[Dict]:
        """提取画像线索"""
        hints = []
        
        # 从图片提取线索
        for indicator in image_result.get('learning_indicators', []):
            hints.append({
                'source': 'image',
                'type': indicator.get('indicator'),
                'value': indicator.get('value'),
                'confidence': indicator.get('confidence', 0.5)
            })
        
        # 从文本提取线索
        if text_result.get('features', {}).get('sentiment'):
            hints.append({
                'source': 'text',
                'type': 'sentiment',
                'value': text_result['features']['sentiment'],
                'confidence': 0.6
            })
        
        return hints
    
    def _calculate_confidence_weight(
        self,
        text_result: Dict,
        image_result: Dict
    ) -> float:
        """计算置信度权重"""
        weight = 0.5  # 基础权重
        
        if text_result.get('content'):
            weight += 0.2
        if image_result.get('image_description') and not image_result.get('error'):
            weight += 0.3
        
        return min(1.0, weight)


def context_text_from_context(context: Dict) -> str:
    """从上下文字典提取文本"""
    parts = []
    if context.get('query'):
        parts.append(f"用户查询: {context['query']}")
    if context.get('conversation_summary'):
        parts.append(f"对话摘要: {context['conversation_summary']}")
    return ' '.join(parts)
