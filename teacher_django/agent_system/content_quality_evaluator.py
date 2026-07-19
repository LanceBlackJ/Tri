"""内容质量评估器

基于COGENT框架的多维评估系统，用于评估生成内容的质量。
"""
import json
import re
from typing import Dict, List

from .services.xinghuo_client import XinghuoClient
from .curriculum_standards import CurriculumStandards


class ContentQualityEvaluator:
    """内容质量评估器"""
    
    CRITERIA = {
        'accuracy': {
            'name': '内容准确性',
            'weight': 0.25,
            'description': '内容是否有事实错误或概念误解',
        },
        'completeness': {
            'name': '知识点完整性',
            'weight': 0.20,
            'description': '是否覆盖核心知识点，无重要遗漏',
        },
        'readability': {
            'name': '可读性',
            'weight': 0.15,
            'description': '语言难度是否适合目标受众',
        },
        'alignment': {
            'name': '课程标准对齐',
            'weight': 0.20,
            'description': '是否符合课程标准要求',
        },
        'educational_value': {
            'name': '教育价值',
            'weight': 0.10,
            'description': '是否能有效促进学习',
        },
        'engagement': {
            'name': '学生参与度',
            'weight': 0.10,
            'description': '是否有互动设计，能激发兴趣',
        },
    }
    
    def __init__(self, grade_level: str = 'college'):
        self.grade_level = grade_level
        self.client = XinghuoClient()
        self.standards_db = CurriculumStandards()
    
    def evaluate(self, topic: str, content: str, content_type: str = 'doc') -> Dict:
        """评估内容质量
        
        Args:
            topic: 课程主题
            content: 待评估的内容
            content_type: 内容类型（doc/ppt/quiz）
        
        Returns:
            评估报告，包含各维度评分和综合评分
        """
        standards = self.standards_db.query_standards(topic, self.grade_level)
        
        # 各维度评估
        accuracy_score = self._evaluate_accuracy(content, topic)
        completeness_score = self._evaluate_completeness(content, topic, standards)
        readability_score = self._evaluate_readability(content)
        alignment_score = self._evaluate_alignment(content, standards)
        educational_score = self._evaluate_educational_value(content, topic)
        engagement_score = self._evaluate_engagement(content)
        
        # 综合评分
        weighted_score = (
            accuracy_score * self.CRITERIA['accuracy']['weight'] +
            completeness_score * self.CRITERIA['completeness']['weight'] +
            readability_score * self.CRITERIA['readability']['weight'] +
            alignment_score * self.CRITERIA['alignment']['weight'] +
            educational_score * self.CRITERIA['educational_value']['weight'] +
            engagement_score * self.CRITERIA['engagement']['weight']
        )
        
        return {
            'overall_score': round(weighted_score, 1),
            'grade': self._get_grade(weighted_score),
            'criteria': {
                'accuracy': {'score': accuracy_score, 'name': '内容准确性'},
                'completeness': {'score': completeness_score, 'name': '知识点完整性'},
                'readability': {'score': readability_score, 'name': '可读性'},
                'alignment': {'score': alignment_score, 'name': '课程标准对齐'},
                'educational_value': {'score': educational_score, 'name': '教育价值'},
                'engagement': {'score': engagement_score, 'name': '学生参与度'},
            },
            'recommendations': self._generate_recommendations(
                accuracy_score, completeness_score, readability_score,
                alignment_score, educational_score, engagement_score
            ),
            'strengths': self._identify_strengths(
                accuracy_score, completeness_score, readability_score,
                alignment_score, educational_score, engagement_score
            ),
        }
    
    def _evaluate_accuracy(self, content: str, topic: str) -> float:
        """评估内容准确性"""
        prompt = f"""
请评估以下课程内容的准确性。

【主题】{topic}

【内容】
{content[:2000]}

【评估维度】
1. 是否有事实错误？
2. 是否有概念误解？
3. 定义是否准确？
4. 公式或代码是否有错误？

【输出格式】
请以JSON格式输出：
{{
  "score": 85,
  "issues": ["问题1", "问题2"],
  "verified_aspects": ["正确方面1"]
}}

直接输出JSON，不要额外说明。
"""
        
        try:
            response = self.client.generate_text(prompt, max_tokens=1024)
            result = json.loads(response)
            return float(result.get('score', 75))
        except Exception:
            return 75.0  # 默认分数
    
    def _evaluate_completeness(self, content: str, topic: str, standards: Dict) -> float:
        """评估知识点完整性"""
        concepts = standards.get('concepts', [])
        objectives = standards.get('learning_objectives', [])
        
        concept_labels = [c.get('label', '') for c in concepts]
        
        # 统计内容中覆盖的概念
        covered_count = 0
        for concept in concept_labels:
            if concept.lower() in content.lower():
                covered_count += 1
        
        if not concept_labels:
            return 75.0
        
        coverage_rate = covered_count / len(concept_labels)
        
        # 根据覆盖率评分
        if coverage_rate >= 0.9:
            return 90.0
        elif coverage_rate >= 0.7:
            return 80.0
        elif coverage_rate >= 0.5:
            return 70.0
        else:
            return 60.0
    
    def _evaluate_readability(self, content: str) -> float:
        """评估可读性"""
        # 基本指标
        sentences = self._split_sentences(content)
        words = self._split_words(content)
        
        if not sentences or not words:
            return 60.0
        
        avg_sentence_length = sum(len(s.split()) for s in sentences) / len(sentences)
        
        # 根据年级水平评估
        max_sentence_length = {
            'primary': 15,
            'junior': 20,
            'senior': 25,
            'college': 35,
            'graduate': 45,
        }.get(self.grade_level, 30)
        
        # 计算可读性分数
        if avg_sentence_length <= max_sentence_length * 0.8:
            return 90.0
        elif avg_sentence_length <= max_sentence_length:
            return 80.0
        elif avg_sentence_length <= max_sentence_length * 1.2:
            return 70.0
        else:
            return 60.0
    
    def _evaluate_alignment(self, content: str, standards: Dict) -> float:
        """评估课程标准对齐度"""
        objectives = standards.get('learning_objectives', [])
        
        if not objectives:
            return 75.0
        
        # 检查目标关键词是否在内容中
        covered_objectives = 0
        for obj in objectives:
            keywords = [w for w in obj.split() if len(w) > 2]
            if any(keyword.lower() in content.lower() for keyword in keywords):
                covered_objectives += 1
        
        coverage = covered_objectives / len(objectives)
        
        if coverage >= 0.8:
            return 90.0
        elif coverage >= 0.6:
            return 80.0
        elif coverage >= 0.4:
            return 70.0
        else:
            return 60.0
    
    def _evaluate_educational_value(self, content: str, topic: str) -> float:
        """评估教育价值"""
        # 检查是否有示例、练习等教育元素
        has_examples = '例如' in content or '比如' in content or '案例' in content
        has_exercises = '练习' in content or '思考' in content or '作业' in content
        has_summary = '总结' in content or '回顾' in content
        
        score = 60.0
        if has_examples:
            score += 10.0
        if has_exercises:
            score += 15.0
        if has_summary:
            score += 15.0
        
        return min(score, 100.0)
    
    def _evaluate_engagement(self, content: str) -> float:
        """评估学生参与度"""
        engagement_elements = {
            'question': content.count('？') + content.count('?'),
            'example': content.count('例如') + content.count('比如'),
            'interactive': '讨论' in content or '思考' in content,
            'story': '故事' in content or '案例' in content,
        }
        
        score = 50.0
        if engagement_elements['question'] >= 2:
            score += 12.5
        if engagement_elements['example'] >= 2:
            score += 12.5
        if engagement_elements['interactive']:
            score += 12.5
        if engagement_elements['story']:
            score += 12.5
        
        return min(score, 100.0)
    
    def _split_sentences(self, text: str) -> List[str]:
        """分割句子"""
        sentences = re.split(r'[。！？\.\?!]+', text)
        return [s.strip() for s in sentences if s.strip()]
    
    def _split_words(self, text: str) -> List[str]:
        """分割词语"""
        pattern = r'[\u4e00-\u9fa5]+|[a-zA-Z]+'
        return [w for w in re.findall(pattern, text) if w]
    
    def _get_grade(self, score: float) -> str:
        """根据分数获取等级"""
        if score >= 90:
            return 'A'
        elif score >= 80:
            return 'B'
        elif score >= 70:
            return 'C'
        elif score >= 60:
            return 'D'
        else:
            return 'F'
    
    def _generate_recommendations(self, accuracy: float, completeness: float,
                                  readability: float, alignment: float,
                                  educational: float, engagement: float) -> List[str]:
        """生成改进建议"""
        recommendations = []
        
        if accuracy < 80:
            recommendations.append('建议核实内容的准确性，纠正可能的事实错误')
        if completeness < 80:
            recommendations.append('建议补充遗漏的重要知识点')
        if readability < 80:
            recommendations.append('建议简化语言，降低句子复杂度')
        if alignment < 80:
            recommendations.append('建议加强对课程标准的学习目标覆盖')
        if educational < 80:
            recommendations.append('建议增加示例和练习环节')
        if engagement < 80:
            recommendations.append('建议增加互动问题和案例设计')
        
        return recommendations
    
    def _identify_strengths(self, accuracy: float, completeness: float,
                           readability: float, alignment: float,
                           educational: float, engagement: float) -> List[str]:
        """识别优势"""
        strengths = []
        
        if accuracy >= 85:
            strengths.append('内容准确可靠')
        if completeness >= 85:
            strengths.append('知识点覆盖全面')
        if readability >= 85:
            strengths.append('语言表达清晰易读')
        if alignment >= 85:
            strengths.append('符合课程标准要求')
        if educational >= 85:
            strengths.append('教育设计完善')
        if engagement >= 85:
            strengths.append('互动性强，能激发学习兴趣')
        
        return strengths
    
    def compare_content(self, content1: str, content2: str, topic: str) -> Dict:
        """比较两个版本的内容质量"""
        eval1 = self.evaluate(topic, content1)
        eval2 = self.evaluate(topic, content2)
        
        return {
            'version1': eval1,
            'version2': eval2,
            'improvement': {
                'overall': round(eval2['overall_score'] - eval1['overall_score'], 1),
                'criteria': {
                    k: round(eval2['criteria'][k]['score'] - eval1['criteria'][k]['score'], 1)
                    for k in eval1['criteria']
                },
            },
            'recommendation': 'version2' if eval2['overall_score'] > eval1['overall_score'] else 'version1',
        }
