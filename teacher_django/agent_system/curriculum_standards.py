"""课程标准数据库模块（基于COGENT框架）

包含学科知识点体系、年级水平约束和课程标准对齐功能。
"""
from typing import Dict, List

# 学科分类
SUBJECTS = {
    'math': '数学',
    'cs': '计算机科学',
    'physics': '物理学',
    'chemistry': '化学',
    'biology': '生物学',
    'english': '英语',
}

# 年级水平定义
GRADE_LEVELS = {
    'primary': {'label': '小学', 'age_range': '6-12', 'vocabulary_level': '基础', 'complexity': '简单'},
    'junior': {'label': '初中', 'age_range': '12-15', 'vocabulary_level': '中等', 'complexity': '中等'},
    'senior': {'label': '高中', 'age_range': '15-18', 'vocabulary_level': '较高', 'complexity': '较高'},
    'college': {'label': '大学', 'age_range': '18+', 'vocabulary_level': '专业', 'complexity': '复杂'},
}

# 知识点体系（简化版）
KNOWLEDGE_CONCEPTS = {
    'math': {
        'algebra': {
            'label': '代数',
            'sub_concepts': ['方程', '函数', '多项式', '指数', '对数'],
            'description': '研究数、数量、关系与结构的数学分支',
            'prerequisites': ['算术', '基本运算'],
        },
        'calculus': {
            'label': '微积分',
            'sub_concepts': ['极限', '导数', '积分', '微分方程'],
            'description': '研究变化率和累积量的数学分支',
            'prerequisites': ['代数', '三角学', '解析几何'],
        },
        'probability': {
            'label': '概率',
            'sub_concepts': ['随机事件', '概率分布', '期望', '方差'],
            'description': '研究随机现象规律的数学分支',
            'prerequisites': ['代数', '组合数学'],
        },
        'linear_algebra': {
            'label': '线性代数',
            'sub_concepts': ['向量', '矩阵', '行列式', '特征值'],
            'description': '研究向量空间和线性变换的数学分支',
            'prerequisites': ['代数', '解析几何'],
        },
    },
    'cs': {
        'programming': {
            'label': '编程基础',
            'sub_concepts': ['变量', '控制结构', '函数', '数据类型'],
            'description': '编写计算机程序的基本技能',
            'prerequisites': [],
        },
        'data_structures': {
            'label': '数据结构',
            'sub_concepts': ['数组', '链表', '栈', '队列', '树', '图'],
            'description': '组织和存储数据的方式',
            'prerequisites': ['编程基础'],
        },
        'algorithms': {
            'label': '算法',
            'sub_concepts': ['排序', '搜索', '动态规划', '贪心算法'],
            'description': '解决问题的步骤和方法',
            'prerequisites': ['编程基础', '数据结构'],
        },
        'machine_learning': {
            'label': '机器学习',
            'sub_concepts': ['监督学习', '无监督学习', '神经网络', '深度学习'],
            'description': '让计算机从数据中学习的方法',
            'prerequisites': ['线性代数', '概率', '微积分', '编程基础'],
        },
    },
    'physics': {
        'mechanics': {
            'label': '力学',
            'sub_concepts': ['牛顿定律', '运动学', '动力学', '能量守恒'],
            'description': '研究物体运动和力的相互作用',
            'prerequisites': ['数学基础'],
        },
        'thermodynamics': {
            'label': '热力学',
            'sub_concepts': ['温度', '热量', '熵', '热力学定律'],
            'description': '研究热现象和能量转换',
            'prerequisites': ['力学'],
        },
        'electromagnetism': {
            'label': '电磁学',
            'sub_concepts': ['电场', '磁场', '电磁感应', '麦克斯韦方程'],
            'description': '研究电和磁的相互作用',
            'prerequisites': ['力学', '微积分'],
        },
    },
}


class CurriculumStandards:
    """课程标准数据库"""
    
    def __init__(self):
        self.concepts = KNOWLEDGE_CONCEPTS
        self.grade_levels = GRADE_LEVELS
    
    def query_standards(self, topic: str, grade_level: str = 'college') -> Dict:
        """查询课程标准
        
        Args:
            topic: 主题名称
            grade_level: 目标年级水平
        
        Returns:
            课程标准字典
        """
        subject = self._detect_subject(topic)
        concepts = self._find_concepts(topic)
        
        return {
            'subject': subject,
            'grade_level': grade_level,
            'grade_info': self.grade_levels.get(grade_level, {}),
            'topic': topic,
            'concepts': concepts,
            'learning_objectives': self._generate_objectives(topic, grade_level),
            'prerequisites': self._find_prerequisites(concepts),
        }
    
    def _detect_subject(self, topic: str) -> str:
        """根据主题检测学科"""
        topic_lower = topic.lower()
        
        if any(kw in topic_lower for kw in ['数学', '代数', '几何', '微积分', '概率', '统计']):
            return 'math'
        if any(kw in topic_lower for kw in ['编程', 'python', '算法', '数据结构', '机器学习', '人工智能']):
            return 'cs'
        if any(kw in topic_lower for kw in ['物理', '力学', '电磁', '热学']):
            return 'physics'
        if any(kw in topic_lower for kw in ['化学', '原子', '分子', '反应']):
            return 'chemistry'
        if any(kw in topic_lower for kw in ['生物', '细胞', '基因', '进化']):
            return 'biology'

        # 识别不出学科时返回 general（此前误默认成 'cs'，会把历史/文学等非计算机主题
        # 也塞进编程/数据结构/机器学习等概念，导致大纲严重跑偏）
        return 'general'
    
    def _find_concepts(self, topic: str) -> List[Dict]:
        """查找相关知识点"""
        subject = self._detect_subject(topic)
        subject_concepts = self.concepts.get(subject, {})
        
        matched = []
        topic_lower = topic.lower()
        
        for key, concept in subject_concepts.items():
            if key.lower() in topic_lower:
                matched.append({
                    'id': key,
                    'label': concept['label'],
                    'description': concept['description'],
                    'sub_concepts': concept['sub_concepts'],
                })
        
        # 注意：不再"没匹配就返回学科全部概念"。以前这么做会把整套学科概念(如 cs 的
        # 编程基础/数据结构/算法/机器学习)硬塞给任意主题，让大纲跑偏(比如 C++ 里冒出"机器学习"章)。
        # 匹配不到具体知识点时返回空，让大模型直接按主题本身生成大纲。
        return matched
    
    def _find_prerequisites(self, concepts: List[Dict]) -> List[str]:
        """查找前置知识"""
        prerequisites = set()
        
        for concept in concepts:
            concept_id = concept.get('id', '')
            subject = self._detect_subject(concept.get('label', ''))
            subject_concepts = self.concepts.get(subject, {})
            
            if concept_id in subject_concepts:
                prerequisites.update(subject_concepts[concept_id].get('prerequisites', []))
        
        return list(prerequisites)
    
    def _generate_objectives(self, topic: str, grade_level: str) -> List[str]:
        """生成学习目标"""
        level_info = self.grade_levels.get(grade_level, {})
        complexity = level_info.get('complexity', '中等')
        
        objectives = []
        
        if complexity == '简单':
            objectives = [
                f'理解{topic}的基本概念',
                f'能够识别{topic}的基本特征',
                f'能解决{topic}的简单问题',
            ]
        elif complexity == '中等':
            objectives = [
                f'深入理解{topic}的核心概念和原理',
                f'能够运用{topic}知识解决实际问题',
                f'能解释{topic}相关现象的原因',
                f'能进行{topic}的基本计算和推导',
            ]
        elif complexity == '较高':
            objectives = [
                f'系统掌握{topic}的理论体系',
                f'能够独立解决{topic}的复杂问题',
                f'能分析{topic}相关问题的多种解决方案',
                f'能将{topic}知识应用到新场景',
            ]
        else:  # 复杂（大学）
            objectives = [
                f'深入理解{topic}的理论基础和前沿发展',
                f'能够进行{topic}的理论推导和证明',
                f'能设计{topic}相关的实验或项目',
                f'能批判性评估{topic}的研究成果',
                f'能将{topic}与其他领域知识融合',
            ]
        
        return objectives
    
    def get_readability_constraints(self, grade_level: str) -> Dict:
        """获取可读性约束"""
        level_info = self.grade_levels.get(grade_level, {})
        
        constraints = {
            'vocabulary_level': level_info.get('vocabulary_level', '中等'),
            'max_sentence_length': {
                'primary': 15,
                'junior': 20,
                'senior': 25,
                'college': 35,
            }.get(grade_level, 25),
            'max_paragraph_length': {
                'primary': 3,
                'junior': 5,
                'senior': 7,
                'college': 10,
            }.get(grade_level, 7),
            'allowed_complexity': level_info.get('complexity', '中等'),
            'examples_required': {
                'primary': 3,
                'junior': 2,
                'senior': 2,
                'college': 1,
            }.get(grade_level, 2),
        }
        
        return constraints


class StandardsAligner:
    """课程标准对齐器"""
    
    def __init__(self):
        self.standards_db = CurriculumStandards()
    
    def align_to_standards(self, topic: str, content: str, grade_level: str = 'college') -> Dict:
        """将内容与课程标准对齐
        
        Args:
            topic: 主题
            content: 待对齐的内容
            grade_level: 目标年级
        
        Returns:
            对齐结果，包含标准匹配度和建议
        """
        standards = self.standards_db.query_standards(topic, grade_level)
        concepts = standards.get('concepts', [])
        objectives = standards.get('learning_objectives', [])
        
        # 分析内容覆盖度
        coverage = self._analyze_coverage(content, concepts, objectives)
        
        return {
            'standards': standards,
            'coverage': coverage,
            'alignment_score': coverage['score'],
            'gaps': coverage['gaps'],
            'recommendations': self._generate_recommendations(coverage, standards),
        }
    
    def _analyze_coverage(self, content: str, concepts: List[Dict], objectives: List[str]) -> Dict:
        """分析内容对知识点和目标的覆盖程度"""
        content_lower = content.lower()
        
        # 计算概念覆盖
        concept_coverage = 0
        covered_concepts = []
        missing_concepts = []
        
        for concept in concepts:
            concept_name = concept.get('label', '').lower()
            sub_concepts = concept.get('sub_concepts', [])
            
            # 检查概念是否被覆盖
            concept_found = concept_name in content_lower
            
            # 检查子概念
            for sub in sub_concepts:
                if sub.lower() in content_lower:
                    concept_found = True
                    break
            
            if concept_found:
                concept_coverage += 1
                covered_concepts.append(concept['label'])
            else:
                missing_concepts.append(concept['label'])
        
        concept_score = (concept_coverage / len(concepts)) * 100 if concepts else 100
        
        # 计算目标覆盖
        objective_coverage = 0
        covered_objectives = []
        missing_objectives = []
        
        for objective in objectives:
            # 简化检查：看目标中的关键词是否在内容中
            keywords = [w for w in objective.replace('{topic}', '').split() if len(w) > 2]
            found = any(keyword.lower() in content_lower for keyword in keywords)
            
            if found:
                objective_coverage += 1
                covered_objectives.append(objective)
            else:
                missing_objectives.append(objective)
        
        objective_score = (objective_coverage / len(objectives)) * 100 if objectives else 100
        
        return {
            'score': (concept_score + objective_score) / 2,
            'concept_coverage': {
                'score': concept_score,
                'covered': covered_concepts,
                'missing': missing_concepts,
            },
            'objective_coverage': {
                'score': objective_score,
                'covered': covered_objectives,
                'missing': missing_objectives,
            },
            'gaps': missing_concepts + missing_objectives,
        }
    
    def _generate_recommendations(self, coverage: Dict, standards: Dict) -> List[str]:
        """生成改进建议"""
        recommendations = []
        gaps = coverage.get('gaps', [])
        
        if gaps:
            recommendations.append(f"建议补充以下内容：{', '.join(gaps[:5])}")
        
        score = coverage['score']
        if score < 60:
            recommendations.append("内容与课程标准差距较大，建议重新审视教学目标")
        elif score < 80:
            recommendations.append("内容基本符合要求，建议补充遗漏的知识点")
        
        return recommendations
