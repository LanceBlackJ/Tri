"""学习效果评估系统

通过实时跟踪学生的学习行为、练习测试情况、资源使用反馈等数据，
依托大模型的数据分析能力实现对学生学习效果的多维度、精准评估；
并根据评估结果及时动态调整学习资源推送策略和学习计划。
"""
import logging
from typing import Dict, List
from django.db.models import Q, Avg
from django.utils import timezone

from agent_system.models import StudentProfile, LearningResource
from curriculum_app.models import LearningProgress, MaterialQuizAttempt, MaterialQuestionStat

logger = logging.getLogger(__name__)


class AssessmentEngine:
    """学习效果评估引擎"""
    
    def __init__(self, user):
        self.user = user
        try:
            self.profile = user.student_profile
        except StudentProfile.DoesNotExist:
            self.profile = None
    
    def assess_overall_progress(self) -> Dict:
        """综合评估学习进度"""
        assessment = {
            'overall_score': 0,
            'detailed_scores': {},
            'recommendations': [],
            'weak_areas': [],
            'strong_areas': [],
            'learning_patterns': {},
        }
        
        # 1. 学习进度评估
        progress_data = self._assess_progress()
        assessment['detailed_scores']['progress'] = progress_data
        assessment['overall_score'] += progress_data['score'] * 0.3
        
        # 2. 练习测试评估
        quiz_data = self._assess_quiz_performance()
        assessment['detailed_scores']['quiz'] = quiz_data
        assessment['overall_score'] += quiz_data['score'] * 0.4
        
        # 3. 资源使用评估
        resource_data = self._assess_resource_usage()
        assessment['detailed_scores']['resource'] = resource_data
        assessment['overall_score'] += resource_data['score'] * 0.3
        
        # 4. 识别薄弱和强项领域
        weak, strong = self._identify_areas()
        assessment['weak_areas'] = weak
        assessment['strong_areas'] = strong
        
        # 5. 生成建议
        assessment['recommendations'] = self._generate_recommendations(assessment)
        
        # 6. 分析学习模式
        assessment['learning_patterns'] = self._analyze_learning_patterns()
        
        # 四舍五入总分
        assessment['overall_score'] = round(assessment['overall_score'], 1)
        
        return assessment
    
    def _assess_progress(self) -> Dict:
        """评估课程学习进度"""
        progress_records = LearningProgress.objects.filter(user=self.user)
        
        if not progress_records.exists():
            return {'score': 0, 'completed_courses': 0, 'total_courses': 0, 'avg_progress': 0}
        
        total_progress = 0
        completed_count = 0
        total_count = 0
        
        for record in progress_records:
            total_count += 1
            if record.status == 'completed':
                completed_count += 1
                total_progress += 100
            elif record.status == 'in_progress':
                if record.total_slides > 0:
                    total_progress += (record.completed_slides / record.total_slides) * 100
                else:
                    total_progress += 50
        
        avg_progress = total_progress / total_count if total_count > 0 else 0
        score = min(avg_progress, 100)
        
        return {
            'score': score,
            'completed_courses': completed_count,
            'total_courses': total_count,
            'avg_progress': round(avg_progress, 1),
        }
    
    def _assess_quiz_performance(self) -> Dict:
        """评估练习测试表现"""
        quiz_attempts = MaterialQuizAttempt.objects.filter(user=self.user)
        
        if not quiz_attempts.exists():
            return {'score': 0, 'attempts': 0, 'avg_score': 0, 'improvement_trend': 'stable'}
        
        avg_score = quiz_attempts.aggregate(Avg('score'))['score__avg'] or 0
        attempts_count = quiz_attempts.count()
        
        # 计算进步趋势
        recent_attempts = quiz_attempts.order_by('-created_at')[:5]
        recent_scores = [a.score for a in recent_attempts]
        
        if len(recent_scores) >= 3:
            improvement = recent_scores[-1] - recent_scores[0]
            if improvement > 5:
                trend = 'improving'
            elif improvement < -5:
                trend = 'declining'
            else:
                trend = 'stable'
        else:
            trend = 'stable'
        
        score = min(avg_score, 100)
        
        return {
            'score': score,
            'attempts': attempts_count,
            'avg_score': round(avg_score, 1),
            'improvement_trend': trend,
        }
    
    def _assess_resource_usage(self) -> Dict:
        """评估资源使用情况"""
        # 分析用户访问的资源类型分布
        # 这里简化处理，基于学习进度中的资源交互来评估
        
        progress_records = LearningProgress.objects.filter(user=self.user)
        total_slides_accessed = sum(r.completed_slides for r in progress_records)
        
        # 获取用户创建的学习资源（作为资源使用的间接指标）
        user_resources = LearningResource.objects.filter(author=self.user)
        resource_count = user_resources.count()
        
        # 计算活跃度分数（基于访问量和资源创建）
        base_score = min(total_slides_accessed * 2, 50)
        resource_score = min(resource_count * 10, 30)
        engagement_score = self._get_engagement_score()
        
        total_score = base_score + resource_score + engagement_score
        
        return {
            'score': min(total_score, 100),
            'slides_accessed': total_slides_accessed,
            'resources_created': resource_count,
            'engagement_score': engagement_score,
        }
    
    def _get_engagement_score(self) -> float:
        """获取参与度分数"""
        if not self.profile:
            return 20
        
        engagement = self.profile.engagement or {}
        return float(engagement.get('score', 20))
    
    def _identify_areas(self) -> tuple:
        """识别薄弱和强项领域"""
        weak_areas = []
        strong_areas = []
        
        # 1. 从知识画像获取
        if self.profile and self.profile.knowledge_profile:
            for topic, level in self.profile.knowledge_profile.items():
                level_str = str(level).lower()
                if level_str in ['初级', '入门', 'low', '0', '1', '2']:
                    weak_areas.append({'topic': topic, 'level': level, 'source': 'profile'})
                elif level_str in ['高级', '精通', 'high', '8', '9', '10']:
                    strong_areas.append({'topic': topic, 'level': level, 'source': 'profile'})
        
        # 2. 从错题统计获取
        question_stats = MaterialQuestionStat.objects.filter(
            user=self.user,
            wrong_count__gt=0
        ).order_by('-wrong_count')[:5]
        
        for stat in question_stats:
            weak_areas.append({
                'topic': stat.knowledge_tag or '未分类',
                'level': f"错误{stat.wrong_count}次",
                'source': 'quiz',
                'question': stat.question_text[:50],
            })
        
        # 3. 从连续错题获取
        consecutive_wrong = MaterialQuestionStat.objects.filter(
            user=self.user,
            consecutive_wrong_count__gte=2
        )
        for stat in consecutive_wrong:
            exists = any(w.get('topic') == (stat.knowledge_tag or '未分类') for w in weak_areas)
            if not exists:
                weak_areas.append({
                    'topic': stat.knowledge_tag or '未分类',
                    'level': f"连续错误{stat.consecutive_wrong_count}次",
                    'source': 'consecutive',
                })
        
        return weak_areas, strong_areas
    
    def _generate_recommendations(self, assessment: Dict) -> List[str]:
        """生成个性化建议"""
        recommendations = []
        overall_score = assessment['overall_score']
        
        # 总体建议
        if overall_score < 30:
            recommendations.append("建议增加学习时间，每天至少学习30分钟")
        elif overall_score < 60:
            recommendations.append("学习进度良好，继续保持，可以适当增加练习量")
        else:
            recommendations.append("学习表现优秀，建议挑战更高难度的内容")
        
        # 进度相关建议
        progress = assessment['detailed_scores'].get('progress', {})
        if progress.get('completed_courses', 0) == 0:
            recommendations.append("建议从第一门课程开始学习")
        
        # 练习相关建议
        quiz = assessment['detailed_scores'].get('quiz', {})
        if quiz.get('attempts', 0) < 3:
            recommendations.append("建议多做练习题来巩固知识")
        if quiz.get('improvement_trend') == 'declining':
            recommendations.append("最近成绩有所下降，建议复习基础概念")
        if quiz.get('improvement_trend') == 'improving':
            recommendations.append("成绩正在提升，继续保持！")
        
        # 薄弱领域建议
        weak_areas = assessment.get('weak_areas', [])
        if weak_areas:
            weak_topics = [w['topic'] for w in weak_areas[:3]]
            recommendations.append(f"建议重点复习：{', '.join(weak_topics)}")
        
        return recommendations
    
    def _analyze_learning_patterns(self) -> Dict:
        """分析学习模式"""
        patterns = {
            'preferred_time': 'unknown',
            'preferred_format': 'unknown',
            'learning_speed': 'average',
            'focus_duration': 'average',
        }
        
        # 根据学习偏好推断
        if self.profile and self.profile.learning_preferences:
            prefs = self.profile.learning_preferences
            if prefs.get('preferred_format'):
                patterns['preferred_format'] = prefs['preferred_format']
        
        # 根据认知风格推断
        if self.profile and self.profile.cognitive_style:
            patterns['learning_style'] = self.profile.cognitive_style
        
        return patterns
    
    def update_profile_based_on_assessment(self, assessment: Dict):
        """根据评估结果更新学生画像"""
        if not self.profile:
            return
        
        # 更新参与度
        engagement = self.profile.engagement or {}
        engagement['score'] = assessment['overall_score']
        engagement['last_assessment'] = timezone.now().isoformat()
        self.profile.engagement = engagement
        
        # 更新知识画像（基于薄弱领域）
        knowledge_profile = self.profile.knowledge_profile or {}
        for weak in assessment.get('weak_areas', []):
            topic = weak['topic']
            if topic not in knowledge_profile or knowledge_profile[topic] in ['初级', '入门']:
                knowledge_profile[topic] = '需要加强'
        
        for strong in assessment.get('strong_areas', []):
            topic = strong['topic']
            knowledge_profile[topic] = '掌握良好'
        
        self.profile.knowledge_profile = knowledge_profile
        
        # 更新易错点
        misconceptions = list(self.profile.misconceptions or [])
        for weak in assessment.get('weak_areas', []):
            topic = weak['topic']
            if topic not in misconceptions:
                misconceptions.append(topic)
        
        self.profile.misconceptions = misconceptions[:20]
        
        self.profile.save()
    
    def generate_adjusted_plan(self, assessment: Dict) -> Dict:
        """根据评估结果生成调整后的学习计划"""
        plan = {
            'recommendations': assessment['recommendations'],
            'focus_areas': [],
            'adjustments': [],
            'resources': [],
        }
        
        # 确定重点关注领域
        weak_areas = assessment.get('weak_areas', [])
        for weak in weak_areas[:3]:
            plan['focus_areas'].append({
                'topic': weak['topic'],
                'reason': weak.get('level', ''),
                'suggestion': self._get_topic_suggestion(weak['topic']),
            })
        
        # 生成调整建议
        overall_score = assessment['overall_score']
        if overall_score < 40:
            plan['adjustments'].append({
                'action': 'reduce_load',
                'description': '减少学习负担，专注于核心内容',
                'details': '建议每天学习时间不超过1小时，优先复习基础知识',
            })
        elif overall_score > 80:
            plan['adjustments'].append({
                'action': 'increase_challenge',
                'description': '增加挑战难度',
                'details': '建议尝试高级课程和项目实践',
            })
        
        # 推荐相关资源
        for area in plan['focus_areas']:
            resources = self._find_resources_for_topic(area['topic'])
            plan['resources'].extend(resources)
        
        return plan
    
    def _get_topic_suggestion(self, topic: str) -> str:
        """获取主题学习建议"""
        suggestions = {
            '线性代数': '建议从向量和矩阵基础开始，配合练习题巩固',
            '机器学习': '建议先掌握线性代数和概率论基础',
            '深度学习': '建议先学习机器学习基础，再深入神经网络',
            '数据结构': '建议结合编程实践学习各种数据结构',
            '算法': '建议从基础算法开始，逐步提高难度',
        }
        return suggestions.get(topic, f"建议系统学习{topic}相关课程")
    
    def _find_resources_for_topic(self, topic: str) -> List[Dict]:
        """查找主题相关资源"""
        resources = LearningResource.objects.filter(
            Q(title__icontains=topic) | Q(tags__contains=[topic])
        ).order_by('-created_at')[:3]
        
        return [
            {
                'id': r.id,
                'title': r.title,
                'type': r.resource_type,
                'preview': r.content[:100],
            }
            for r in resources
        ]


class AdaptiveLearningEngine:
    """自适应学习引擎
    
    根据评估结果动态调整学习路径和资源推送。
    """
    
    def __init__(self, user):
        self.user = user
        self.assessment_engine = AssessmentEngine(user)
    
    def run_adaptive_cycle(self) -> Dict:
        """运行自适应学习周期"""
        # 1. 评估当前状态
        assessment = self.assessment_engine.assess_overall_progress()
        
        # 2. 更新学生画像
        self.assessment_engine.update_profile_based_on_assessment(assessment)
        
        # 3. 生成调整计划
        adjusted_plan = self.assessment_engine.generate_adjusted_plan(assessment)
        
        # 4. 返回综合结果
        return {
            'assessment': assessment,
            'adjusted_plan': adjusted_plan,
            'timestamp': timezone.now().isoformat(),
        }
    
    def suggest_next_step(self) -> Dict:
        """建议下一步学习行动"""
        assessment = self.assessment_engine.assess_overall_progress()
        weak_areas = assessment.get('weak_areas', [])
        strong_areas = assessment.get('strong_areas', [])
        
        if weak_areas:
            # 优先建议薄弱领域
            topic = weak_areas[0]['topic']
            return {
                'action': 'review',
                'topic': topic,
                'reason': f"{topic}是薄弱领域，建议优先复习",
                'resources': self._get_topic_resources(topic),
            }
        
        # 如果没有薄弱领域，建议拓展学习
        return {
            'action': 'expand',
            'topic': '拓展学习',
            'reason': '当前学习状况良好，建议探索新的学习内容',
            'resources': self._get_recommended_resources(),
        }
    
    def _get_topic_resources(self, topic: str) -> List[Dict]:
        """获取主题相关资源"""
        return self.assessment_engine._find_resources_for_topic(topic)
    
    def _get_recommended_resources(self) -> List[Dict]:
        """获取推荐资源"""
        resources = LearningResource.objects.filter(
            Q(resource_type='doc') | Q(resource_type='video')
        ).order_by('-created_at')[:5]
        
        return [
            {
                'id': r.id,
                'title': r.title,
                'type': r.resource_type,
                'preview': r.content[:100],
            }
            for r in resources
        ]