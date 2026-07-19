import json
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings

from .services.xinghuo_client import XinghuoClient

logger = logging.getLogger(__name__)

SLIDE_LAYOUTS = {
    'cover',
    'agenda',
    'concept_map',
    'two_column',
    'process_flow',
    'case_study',
    'comparison',
    'quiz_check',
    'summary',
    'animation_embed',
    'custom',
}
SLIDE_THEMES = {'academic_light', 'tech_blue', 'chalkboard_dark'}


def _normalize_slide_layout(value: Any, fallback: str = 'two_column') -> str:
    layout = str(value or '').strip().lower().replace('-', '_')
    aliases = {
        'concept': 'concept_map',
        'case': 'case_study',
        'practice': 'quiz_check',
        'animation': 'animation_embed',
        'flow': 'process_flow',
        'compare': 'comparison',
    }
    layout = aliases.get(layout, layout)
    return layout if layout in SLIDE_LAYOUTS else fallback


def _normalize_slide_theme(value: Any) -> str:
    theme = str(value or '').strip().lower().replace('-', '_')
    return theme if theme in SLIDE_THEMES else 'academic_light'


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    text = (text or '').strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    fence_match = re.search(r"```(?:json|JSON)?\s*\n([\s\S]*?)```", text)
    if fence_match:
        return fence_match.group(1).strip()
    if text.startswith('```'):
        text = re.sub(r"^```(?:json|JSON)?\s*\n?", '', text)
        text = re.sub(r"\n?```\s*$", '', text)
    return text.strip()


def _repair_truncated_json(text: str) -> Any:
    """尝试修复被截断的 JSON 文本：在每个"安全截断点"（字符串/容器结束或逗号）
    处尝试补全未闭合的括号并重新解析，从最靠后的安全点开始尝试。"""
    text = text.strip()
    if not text:
        return None

    stack: List[str] = []
    in_string = False
    escape = False
    safe_points: List[tuple] = []
    for index, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
                safe_points.append((index + 1, list(stack)))
            continue
        if ch == '"':
            in_string = True
        elif ch in '{[':
            stack.append(ch)
        elif ch in '}]':
            if stack:
                stack.pop()
            safe_points.append((index + 1, list(stack)))
        elif ch == ',':
            safe_points.append((index, list(stack)))

    for cut, stack_at_cut in reversed(safe_points):
        candidate = text[:cut].rstrip()
        candidate = candidate.rstrip(',').rstrip()
        if candidate.endswith(':'):
            continue
        repaired = candidate
        for opener in reversed(stack_at_cut):
            repaired += '}' if opener == '{' else ']'
        try:
            return json.loads(repaired)
        except Exception:
            continue
    return None


def _extract_json_object_robust(text: Any) -> Optional[Dict[str, Any]]:
    """比 `_extract_json_object` 更健壮的 JSON 提取：去除 Markdown 代码围栏，
    并对被截断的 JSON 输出尝试括号配平修复后重新解析。"""
    if isinstance(text, dict):
        return text
    text = str(text or '').strip()
    if not text:
        return None

    candidates = []
    cleaned = _strip_markdown_fences(text)
    if cleaned and cleaned != text:
        candidates.append(cleaned)
    candidates.append(text)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        match = re.search(r"\{[\s\S]*\}", candidate)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass

        brace_index = candidate.find('{')
        if brace_index != -1:
            repaired = _repair_truncated_json(candidate[brace_index:])
            if isinstance(repaired, dict):
                return repaired

    return None


def _as_text_list(value: Any, limit: int = 4) -> List[str]:
    if isinstance(value, list):
        items = value
    elif value:
        items = [value]
    else:
        items = []
    normalized = []
    for item in items:
        text = str(item or '').strip()
        if text:
            normalized.append(text)
        if len(normalized) >= limit:
            break
    return normalized


def _topic_teaching_profile(topic: str, outline_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """根据主题返回学科特定的教学内容档案：优先匹配预置的学科专属模板，
    未命中时基于curriculum_standards模块生成通用档案；再用课程蓝图中各章节的
    curiosity_hook/objectives/summary 覆盖动机/目标/小结部分。"""
    compact = re.sub(r'\s+', '', str(topic or ''))

    profile: Dict[str, Any] = {}

    # 机器学习/深度学习主题
    if '梯度下降' in compact:
        profile = {
            'objectives': [
                '看懂梯度下降为什么能让损失函数一点点变小',
                '掌握负梯度方向、学习率、迭代更新这三个关键点',
                '能用一个简单函数或线性回归例子说明它怎么工作',
            ],
            'why_bullets': [
                '很多模型最终都要把"误差尽量变小"转成参数优化问题',
                '梯度下降提供了一种沿着误差下降方向逐步更新参数的通用方法',
                '学它的关键不是背公式，而是理解"方向、步长、收敛"三件事',
            ],
            'why_visual_blocks': [
                {'kind': 'bullet_card', 'label': '没有它', 'items': ['很难系统地调整参数让损失函数下降', '会知道误差大，却不知道该往哪个方向改参数', '训练过程容易停留在试错而不是可解释的优化']},
                {'kind': 'bullet_card', 'label': '学会它', 'items': ['能解释参数为什么沿负梯度方向更新', '能判断学习率过大或过小会带来什么问题', '能把它迁移到线性回归、神经网络等模型训练里']},
            ],
            'concept_bullets': [
                '梯度下降是在损失函数上不断朝负梯度方向移动参数的迭代优化方法',
                '梯度告诉我们当前位置上升最快的方向，因此负梯度就是下降最快的局部方向',
                '一次更新至少要同时看当前参数、梯度大小和学习率设定',
            ],
            'concept_visual_blocks': [
                {'kind': 'concept_node', 'label': '对象', 'text': '模型参数和它对应的损失函数'},
                {'kind': 'concept_node', 'label': '作用', 'text': '根据梯度决定参数应该往哪个方向更新'},
                {'kind': 'concept_node', 'label': '结果', 'text': '让损失逐步下降，逼近较优解'},
                {'kind': 'concept_node', 'label': '边界', 'text': '不保证一步到最优，也可能遇到局部最优或学习率不合适'},
            ],
            'process_bullets': [
                '先定义损失函数，明确"误差多大才算不好"',
                '在当前参数位置计算梯度，判断误差上升最快的方向',
                '沿负梯度方向按学习率更新参数，再重复这个过程直到损失趋于稳定',
            ],
            'process_visual_blocks': [
                {'kind': 'step', 'label': '1. 定义目标', 'text': '先写出损失函数，知道要最小化什么'},
                {'kind': 'step', 'label': '2. 计算梯度', 'text': '看当前参数位置下，损失对每个参数的变化趋势'},
                {'kind': 'step', 'label': '3. 更新参数', 'text': '用 参数 = 参数 - 学习率 × 梯度 完成一次迭代'},
                {'kind': 'step', 'label': '4. 检查收敛', 'text': '观察损失是否继续下降，梯度是否接近 0'},
            ],
            'case_bullets': [
                '在线性回归里，可以把均方误差当作损失函数，用梯度下降更新斜率和截距',
                '如果当前预测值整体偏大，梯度会推动参数往减小误差的方向调整',
                '多次迭代后，拟合直线会逐渐贴近训练样本',
            ],
            'case_visual_blocks': [
                {'kind': 'case', 'label': '案例情境', 'text': '用梯度下降训练一条线性回归直线，让预测房价更接近真实值'},
                {'kind': 'bullet_card', 'label': '分析路径', 'items': ['先算当前模型的均方误差', '再看斜率和截距对应的梯度方向', '按学习率更新后重新计算误差']},
                {'kind': 'bullet_card', 'label': '结论表达', 'items': ['误差下降说明方向基本正确', '震荡往往意味着学习率过大', '下降太慢通常意味着学习率过小']},
            ],
            'comparison_bullets': [
                '梯度下降不是直接求解析解，而是通过迭代逐步逼近较优参数',
                '梯度为 0 不一定代表全局最优，还可能是局部最优或鞍点附近',
                '学习率不是越大越快，过大会震荡甚至发散',
            ],
            'comparison_visual_blocks': [
                {'kind': 'compare_column', 'label': '正确理解', 'items': ['负梯度决定方向，学习率决定步长', '每次更新后都要重新计算梯度', '关注损失是否整体下降而不是只看某一步']},
                {'kind': 'compare_column', 'label': '常见误区', 'items': ['把梯度本身当成更新后的参数', '认为学习率越大收敛越快', '看到梯度接近 0 就断定一定是最优解']},
            ],
            'quiz_bullets': [
                '为什么更新方向要取负梯度，而不是沿梯度方向前进？',
                '学习率过大时，损失函数曲线通常会出现什么现象？',
                '在线性回归场景里，梯度下降每次迭代到底更新了哪些量？',
            ],
            'summary_bullets': [
                '梯度下降的核心是用梯度判断方向，用学习率控制步长',
                '它通过多次迭代让损失函数逐步下降，而不是一次得到答案',
                '真正学会的标志是能解释更新公式，并能判断学习率和收敛现象',
            ],
        }
    
    # Python编程主题
    elif 'Python' in compact or 'python' in compact.lower():
        profile = {
            'objectives': [
                '理解Python的核心语法和编程思维',
                '掌握变量、数据类型、控制流程等基础概念',
                '能独立编写简单的Python程序解决实际问题',
            ],
            'why_bullets': [
                'Python是人工智能、数据分析领域最流行的编程语言',
                '语法简洁，适合快速原型开发和教学入门',
                '强大的第三方库生态让复杂任务变得简单',
            ],
            'concept_bullets': [
                'Python是一种解释型、面向对象的高级编程语言',
                '采用缩进语法，代码可读性强',
                '支持多种编程范式：面向对象、函数式、过程式',
            ],
            'process_bullets': [
                '明确要解决的问题，分析需求',
                '设计数据结构和算法思路',
                '编写代码实现功能',
                '测试调试，验证结果正确性',
            ],
            'case_bullets': [
                '用Python计算列表中所有数字的平均值',
                '编写程序判断一个数是否为质数',
                '读取文件内容并统计词频',
            ],
            'comparison_bullets': [
                'Python不需要声明变量类型，但要注意类型转换',
                '缩进不是风格问题，而是语法的一部分',
                '列表和元组的区别：可变vs不可变',
            ],
            'quiz_bullets': [
                'Python中==和is的区别是什么？',
                '列表推导式的语法格式是什么？',
                'try-except语句的作用是什么？',
            ],
            'summary_bullets': [
                'Python以其简洁语法和丰富库生态成为首选语言',
                '掌握基础语法后可以快速上手数据分析、Web开发等领域',
                '实践是学习编程的关键，多写代码才能真正掌握',
            ],
        }
    
    # 数据结构主题
    elif '数据结构' in compact or 'datastructure' in compact.lower():
        profile = {
            'objectives': [
                '理解常见数据结构的特点和适用场景',
                '掌握数组、链表、栈、队列、树、图的基本操作',
                '能根据问题选择合适的数据结构',
            ],
            'why_bullets': [
                '数据结构是程序的骨架，决定了数据的组织方式',
                '选择合适的数据结构能显著提升程序效率',
                '是算法设计和系统开发的基础',
            ],
            'concept_bullets': [
                '数据结构是计算机存储、组织数据的方式',
                '分为线性结构（数组、链表、栈、队列）和非线性结构（树、图）',
                '每种结构都有其时间复杂度和空间复杂度特征',
            ],
            'process_bullets': [
                '分析问题需求和数据访问模式',
                '评估不同数据结构的时间空间复杂度',
                '选择最适合的结构并实现',
                '测试验证性能和正确性',
            ],
            'case_bullets': [
                '用栈实现括号匹配检查',
                '用队列实现消息队列',
                '用二叉搜索树实现快速查找',
            ],
            'comparison_bullets': [
                '数组随机访问快但插入删除慢，链表相反',
                '栈是后进先出(LIFO)，队列是先进先出(FIFO)',
                '顺序查找vs二分查找的适用场景',
            ],
            'quiz_bullets': [
                '数组和链表的主要区别是什么？',
                '二叉搜索树的查找时间复杂度是多少？',
                '什么情况下应该用栈而不是队列？',
            ],
            'summary_bullets': [
                '数据结构是程序的基石，直接影响性能',
                '没有最好的数据结构，只有最适合场景的',
                '理解每种结构的优缺点是做出正确选择的关键',
            ],
        }
    
    # 计算机网络主题
    elif '计算机网络' in compact or 'network' in compact.lower():
        profile = {
            'objectives': [
                '理解计算机网络的分层体系结构',
                '掌握TCP/IP协议栈的工作原理',
                '能解释数据在网络中的传输过程',
            ],
            'why_bullets': [
                '网络是现代信息技术的基础设施',
                '理解网络原理能更好地设计和维护系统',
                '是云计算、分布式系统的基础',
            ],
            'concept_bullets': [
                '计算机网络是自主计算机的互连集合',
                'OSI七层模型：物理层、数据链路层、网络层、传输层、会话层、表示层、应用层',
                'TCP/IP是事实上的工业标准',
            ],
            'process_bullets': [
                '数据从应用层开始逐层封装',
                '通过物理介质传输到目标主机',
                '目标主机逐层解封装',
                '最终送达应用程序',
            ],
            'case_bullets': [
                '浏览器访问网页的完整过程',
                'TCP三次握手建立连接',
                'DNS域名解析的工作流程',
            ],
            'comparison_bullets': [
                'TCP是可靠传输，UDP是不可靠传输',
                'HTTP和HTTPS的区别：明文vs加密',
                'IPv4和IPv6的地址空间差异',
            ],
            'quiz_bullets': [
                'TCP三次握手的目的是什么？',
                'HTTP状态码301和302有什么区别？',
                'DNS解析过程中可能遇到哪些问题？',
            ],
            'summary_bullets': [
                '网络分层设计降低了复杂性',
                'TCP/IP协议栈是互联网的核心',
                '理解网络原理能帮助排查网络问题',
            ],
        }
    
    # 数据库主题
    elif '数据库' in compact or 'database' in compact.lower():
        profile = {
            'objectives': [
                '理解数据库的基本概念和分类',
                '掌握关系型数据库的设计原则',
                '能编写基本的SQL查询语句',
            ],
            'why_bullets': [
                '数据库是应用程序的数据存储核心',
                '良好的数据库设计是系统性能的关键',
                '数据管理是所有信息系统的基础',
            ],
            'concept_bullets': [
                '数据库是长期存储、管理数据的集合',
                '关系型数据库用表和关系组织数据',
                'SQL是标准的数据查询语言',
            ],
            'process_bullets': [
                '需求分析，确定数据实体和关系',
                '设计ER图和表结构',
                '编写DDL创建表',
                '编写DML进行数据操作',
            ],
            'case_bullets': [
                '设计一个学生管理系统的数据库',
                '查询某个学生的所有课程成绩',
                '统计每个班级的平均分数',
            ],
            'comparison_bullets': [
                '关系型数据库vs非关系型数据库的适用场景',
                '主键和外键的作用区别',
                '索引的作用和代价',
            ],
            'quiz_bullets': [
                '什么是数据库范式？为什么重要？',
                'LEFT JOIN和INNER JOIN的区别是什么？',
                '事务的ACID特性指的是什么？',
            ],
            'summary_bullets': [
                '数据库设计需要遵循规范化原则',
                '索引能加速查询但会降低写入性能',
                '事务保证了数据的一致性和完整性',
            ],
        }
    
    # 操作系统主题
    elif '操作系统' in compact or 'operatingsystem' in compact.lower():
        profile = {
            'objectives': [
                '理解操作系统的基本功能和组成',
                '掌握进程管理、内存管理的基本原理',
                '了解文件系统和设备管理',
            ],
            'why_bullets': [
                '操作系统是硬件和应用程序之间的桥梁',
                '理解OS能更好地编写高效程序',
                '是深入理解计算机系统的关键',
            ],
            'concept_bullets': [
                '操作系统管理计算机硬件资源',
                '主要功能：进程管理、内存管理、文件管理、设备管理',
                '提供用户与计算机交互的接口',
            ],
            'process_bullets': [
                '进程从创建到终止的完整生命周期',
                '内存分配和回收的基本流程',
                '文件的打开、读写、关闭操作',
            ],
            'case_bullets': [
                '进程调度算法的比较',
                '页面置换算法的工作原理',
                '死锁的产生和解决方法',
            ],
            'comparison_bullets': [
                '进程和线程的区别',
                '虚拟内存和物理内存的关系',
                '不同文件系统的特点',
            ],
            'quiz_bullets': [
                '什么是进程上下文切换？',
                '分页和分段的区别是什么？',
                '死锁的四个必要条件是什么？',
            ],
            'summary_bullets': [
                '操作系统是资源管理器和抽象层',
                '进程管理决定了系统的并发性',
                '内存管理让程序能使用比实际更大的地址空间',
            ],
        }
    
    # 软件工程主题
    elif '软件工程' in compact or 'softwareengineering' in compact.lower():
        profile = {
            'objectives': [
                '理解软件工程的基本原理和方法',
                '掌握软件开发流程和质量保障',
                '能应用工程化方法开发软件',
            ],
            'why_bullets': [
                '软件工程让软件开发从个体行为变成团队协作',
                '系统化方法提高了软件质量和可维护性',
                '是大型软件项目成功的保障',
            ],
            'concept_bullets': [
                '软件工程是应用工程原则开发软件的学科',
                '包含需求分析、设计、实现、测试、维护等阶段',
                '核心目标是按时、按预算交付高质量软件',
            ],
            'process_bullets': [
                '需求分析：明确用户需求',
                '系统设计：架构和模块划分',
                '编码实现：编写高质量代码',
                '测试验证：确保功能正确',
            ],
            'case_bullets': [
                '敏捷开发流程的实践',
                '代码审查的实施方法',
                '持续集成和持续交付',
            ],
            'comparison_bullets': [
                '瀑布模型vs敏捷开发的适用场景',
                '单元测试vs集成测试vs系统测试',
                '代码质量和开发速度的平衡',
            ],
            'quiz_bullets': [
                '敏捷开发的核心价值观是什么？',
                '什么是代码覆盖率？为什么重要？',
                '版本控制系统的作用是什么？',
            ],
            'summary_bullets': [
                '软件工程是系统化的软件开发方法',
                '质量保障贯穿整个开发周期',
                '团队协作和工具链是成功的关键',
            ],
        }

    if not profile:
        profile = _generic_teaching_profile(topic)

    chapters = _get_blueprint_chapters(outline_data)
    curiosity_hooks = _as_text_list(
        [c.get('curiosity_hook') for c in chapters if isinstance(c, dict)], limit=3
    )
    if curiosity_hooks:
        profile['why_bullets'] = curiosity_hooks

    chapter_objectives: List[str] = []
    for chapter in chapters:
        if isinstance(chapter, dict):
            chapter_objectives.extend(_as_text_list(chapter.get('objectives'), limit=3))
    if chapter_objectives:
        profile['objectives'] = chapter_objectives[:4]

    chapter_summaries = _as_text_list(
        [c.get('summary') for c in chapters if isinstance(c, dict)], limit=4
    )
    if chapter_summaries:
        profile['summary_bullets'] = chapter_summaries[:3]

    return profile


def _generic_teaching_profile(topic: str) -> Dict[str, Any]:
    """当主题未命中预置专属模板时，基于curriculum_standards模块生成通用教学档案"""
    from .curriculum_standards import CurriculumStandards

    standards_db = CurriculumStandards()
    standards = standards_db.query_standards(topic, 'college')

    concepts = standards.get('concepts', [])
    objectives = standards.get('learning_objectives', [])

    profile: Dict[str, Any] = {
        'objectives': objectives or [
            f'理解{topic}的核心概念',
            f'掌握{topic}的基本原理',
            f'能够应用{topic}解决实际问题',
        ],
        'why_bullets': [
            f'{topic}是{standards.get("subject", "该学科")}领域的重要概念',
            '理解它能帮助我们解决相关的实际问题',
            '掌握它是学习后续内容的基础',
        ],
        'concept_bullets': [],
        'process_bullets': [
            '识别问题和相关条件',
            f'应用{topic}的核心原理',
            '按照步骤执行分析或操作',
            '验证结果是否符合预期',
        ],
        'case_bullets': [
            f'选择一个涉及{topic}的真实场景',
            '分析场景中的关键要素和条件',
            f'应用{topic}的原理进行判断或操作',
            '总结案例中的关键经验和启示',
        ],
        'comparison_bullets': [
            f'不要把{topic}和相近概念混淆',
            '理解它的适用条件和边界',
            '避免只记忆表面关键词',
        ],
        'quiz_bullets': ([
            f'请解释{topic}的核心概念是什么？',
            f'{topic}的主要应用场景有哪些？',
            f'学习{topic}需要哪些前置知识？',
        ] if objectives else [
            f'用一句话解释{topic}',
            f'说出{topic}起作用的关键步骤',
            '给一个例子并说明判断依据',
        ]),
        'summary_bullets': [
            f'{topic}解决了特定的问题或矛盾',
            '理解它需要掌握对象、作用和结果',
            '通过案例和练习可以加深理解',
        ],
    }

    for concept in concepts:
        description = concept.get('description', '')
        sub_concepts = concept.get('sub_concepts', [])

        if description:
            profile['concept_bullets'].append(description)

        if sub_concepts:
            profile['concept_bullets'].extend(sub_concepts[:3])

    if not profile['concept_bullets']:
        profile['concept_bullets'] = [
            f'{topic}是一个核心概念，涉及多个关键要素',
            '理解它需要掌握其定义、作用和适用场景',
            '它与其他概念之间存在密切关系',
        ]

    return profile


_PLACEHOLDER_PREFIXES = (
    '这里可以', '此处可以', '可以在这里', '可以绘制', '可以添加', '可以展示',
    '在这里', '此处展示', '展示一个', '展示一张', '插入一个', '插入一张',
    '示意图', '示意图可', '流程图可', 'here you can', 'insert here',
    '待填', '待补充', '（待', '(待', '【待',
    '[占位',  # XinghuoClient API 失败时返回 [占位讲解]…/[占位流]…，绝不能当真实教学内容渲染
)

def _is_placeholder_text(text: str) -> bool:
    """检测 LLM 生成的元指令占位文本，这类文本不应渲染为教学内容。"""
    if not text:
        return True
    t = text.strip()
    return any(t.startswith(p) for p in _PLACEHOLDER_PREFIXES) or len(t) < 8


def _is_llm_failure(raw: Any) -> bool:
    """判断一次 generate_text/stream 结果是否其实是客户端的失败占位。

    XinghuoClient 在 API 未配置/请求异常时不抛错，而是返回 `[占位讲解]…`
    （xinghuo_client.py:_placeholder_text）或 `[占位流]…`。若不识别它，
    任何“失败即兜底”的逻辑都会在网络抖动时静默失效、把占位文本写进 PPT。
    """
    if not raw:
        return True
    return str(raw).lstrip().startswith('[占位')


# prompt 里的 JSON 示例占位词——大模型有时会把示例原样回显成内容
_SLIDE_EXAMPLE_MARKERS = {
    '完整讲稿，不少于原稿长度', '完整讲稿', '完整句子', '完整句子1', '完整句子2',
    '要点1', '要点2', '...', '（无）', '(无)',
}


def _slide_content_is_placeholder(content: Any) -> bool:
    """检测大模型把 prompt 里的 JSON 示例(如 speaker_notes='完整讲稿，不少于原稿长度'、
    bullets=['完整句子',...])原样回显成内容的情况——这类内容没有教学价值，应视为生成失败。"""
    if not isinstance(content, dict):
        return False
    notes = str(content.get('speaker_notes') or '').strip()
    if notes in _SLIDE_EXAMPLE_MARKERS:
        return True
    bullets = content.get('bullets')
    if isinstance(bullets, list) and bullets:
        stripped = [str(b).strip() for b in bullets]
        if stripped and all(b in _SLIDE_EXAMPLE_MARKERS for b in stripped):
            return True
    return False


def _slide_one_line(skeleton_slide: Dict[str, Any]) -> str:
    """取一页的一句话摘要；缺失时用 teaching_goal / core_explanation 截断兜底。"""
    if not isinstance(skeleton_slide, dict):
        return ''
    one_line = str(skeleton_slide.get('one_line') or '').strip()
    if one_line:
        return one_line[:30]
    fallback = str(
        skeleton_slide.get('teaching_goal')
        or skeleton_slide.get('core_explanation')
        or skeleton_slide.get('title')
        or ''
    ).strip()
    return fallback[:30]


def _build_deck_narrative(skeleton: List[Dict[str, Any]]) -> None:
    """原地给每页骨架挂上 narrative_context（全课叙事线 + 本页位置 + 前后页摘要）。

    纯 Python，不调 LLM、不会失败/截断。注入的是 input-only 文本，不增加任何
    输出侧的截断点，却让每页在并行生成时都能看到全课叙事、避免与相邻页重复。
    """
    if not skeleton:
        return
    n = len(skeleton)
    outline = '\n'.join(
        f'第{i + 1}页《{str(s.get("title") or "").strip()}》— {_slide_one_line(s)}'
        for i, s in enumerate(skeleton)
    )
    for i, s in enumerate(skeleton):
        if not isinstance(s, dict):
            continue
        prev_slide = skeleton[i - 1] if i > 0 else None
        next_slide = skeleton[i + 1] if i < n - 1 else None
        s['narrative_context'] = {
            'deck_outline': outline,
            'position': f'第{i + 1}/{n}页',
            'prev': {
                'title': str(prev_slide.get('title') or '').strip(),
                'one_line': _slide_one_line(prev_slide),
            } if isinstance(prev_slide, dict) else None,
            'next': {
                'title': str(next_slide.get('title') or '').strip(),
                'one_line': _slide_one_line(next_slide),
            } if isinstance(next_slide, dict) else None,
        }


def _format_narrative_context(ctx: Optional[Dict[str, Any]]) -> str:
    """把 narrative_context 拼成注入逐页 prompt 的简短文本块（保持短，避免挤占输出预算）。"""
    if not isinstance(ctx, dict):
        return ''
    out = [
        '## 全局叙事上下文（据此写出承前启后、不与相邻页重复的内容）',
        f'本页位置：{ctx.get("position", "")}',
        '全课叙事线：',
        str(ctx.get('deck_outline') or ''),
    ]
    prev = ctx.get('prev')
    if isinstance(prev, dict) and prev.get('title'):
        out.append(f'上一页《{prev["title"]}》已讲：{prev.get("one_line", "")}——至多一句带过，不要重复展开。')
    nxt = ctx.get('next')
    if isinstance(nxt, dict) and nxt.get('title'):
        out.append(f'下一页《{nxt["title"]}》将讲：{nxt.get("one_line", "")}——结尾可自然引出，但不要提前讲。')
    out.append('只讲属于本页的内容，与相邻页不重叠。')
    return '\n'.join(out)


def _char_ngrams(text: str, n: int = 3) -> set:
    """取字符级 n-gram 集合，用于跨页近重复检测（对中文短文本鲁棒）。"""
    t = ''.join((text or '').split())
    if len(t) < n:
        return {t} if t else set()
    return {t[i:i + n] for i in range(len(t) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _dedupe_bullets_across_slides(slide_payloads: List[Dict[str, Any]], threshold: float = 0.8) -> None:
    """跨页硬去重：用字符 n-gram + Jaccard 删除近重复的要点。

    给"反重复是软 prompt 约束"这条弱模型兜不住的风险一个确定性硬保证。
    守卫：一页去重后若为空则保留其原始 bullets（绝不把一页 bullets 清空）。
    """
    seen: List[set] = []
    for slide in slide_payloads:
        if not isinstance(slide, dict):
            continue
        bullets = slide.get('bullets')
        if not isinstance(bullets, list) or not bullets:
            continue
        kept = []
        kept_grams = []
        for bullet in bullets:
            grams = _char_ngrams(str(bullet), 3)
            if any(_jaccard(grams, other) >= threshold for other in seen):
                continue
            kept.append(bullet)
            kept_grams.append(grams)
        if kept:
            slide['bullets'] = kept
            seen.extend(kept_grams)
        else:
            # 守卫：不允许清空一页；保留原样并登记其 grams
            seen.extend(_char_ngrams(str(b), 3) for b in bullets)


def _default_visual_blocks(layout: str, title: str, bullets: List[str]) -> List[Dict[str, Any]]:
    if layout == 'process_flow':
        return [{'kind': 'step', 'label': item, 'text': item} for item in bullets[:4]]
    if layout == 'comparison':
        left = bullets[0] if bullets else '概念 A'
        right = bullets[1] if len(bullets) > 1 else '概念 B'
        return [
            {'kind': 'compare_column', 'label': left, 'items': bullets[::2] or [left]},
            {'kind': 'compare_column', 'label': right, 'items': bullets[1::2] or [right]},
        ]
    if layout == 'quiz_check':
        return [{'kind': 'question', 'label': '自测', 'text': bullets[0] if bullets else f'请用一句话解释"{title}"。'}]
    if layout == 'concept_map':
        return [{'kind': 'concept_node', 'label': item, 'text': item} for item in ([title] + bullets[:3])]
    return [{'kind': 'bullet_card', 'label': f'要点 {index}', 'text': item} for index, item in enumerate(bullets[:4], start=1)]


def _normalize_visual_blocks(value: Any, layout: str, title: str, bullets: List[str]) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return _default_visual_blocks(layout, title, bullets)
    blocks = []
    for index, block in enumerate(value[:6], start=1):
        if isinstance(block, str):
            blocks.append({'kind': 'bullet_card', 'label': f'视觉块 {index}', 'text': block})
            continue
        if not isinstance(block, dict):
            continue
        text_items = _as_text_list(block.get('items') or block.get('points'), limit=5)
        raw_text = str(block.get('text') or block.get('content') or '').strip()
        # 元指令占位文本：用对应 bullet 内容替换，避免渲染"这里可以绘制..."这类无效文本
        if _is_placeholder_text(raw_text):
            fallback_bullet = bullets[index - 1] if index - 1 < len(bullets) else ''
            raw_text = fallback_bullet
        normalized = {
            'kind': str(block.get('kind') or block.get('type') or 'bullet_card').strip() or 'bullet_card',
            'label': str(block.get('label') or block.get('title') or f'视觉块 {index}').strip(),
            'text': raw_text,
        }
        if text_items:
            normalized['items'] = text_items
        # 保留代码和动画相关字段
        if block.get('code'):
            normalized['code'] = block.get('code')
        if block.get('animation_code'):
            normalized['animation_code'] = block.get('animation_code')
        if block.get('language'):
            normalized['language'] = block.get('language')
        if block.get('usage_note'):
            normalized['usage_note'] = block.get('usage_note')
        if block.get('concept_name'):
            normalized['concept_name'] = str(block.get('concept_name')).strip()
        if block.get('animation_type'):
            normalized['animation_type'] = str(block.get('animation_type')).strip()

        # 保留quiz题目相关字段
        if block.get('kind') == 'question' or block.get('type') == 'question':
            # 保留题目文本
            if block.get('question_text'):
                normalized['question_text'] = str(block.get('question_text')).strip()
            # 保留题目类型
            if block.get('question_type'):
                normalized['question_type'] = str(block.get('question_type')).strip()
            # 保留选项
            if block.get('choices'):
                choices = block.get('choices')
                if isinstance(choices, list):
                    normalized['choices'] = choices
            # 保留正确答案
            if block.get('correct_answer'):
                normalized['correct_answer'] = str(block.get('correct_answer')).strip()
            elif block.get('answer'):
                normalized['correct_answer'] = str(block.get('answer')).strip()
            # 保留解析
            if block.get('explanation'):
                normalized['explanation'] = str(block.get('explanation')).strip()
        
        blocks.append(normalized)
    return blocks or _default_visual_blocks(layout, title, bullets)


def _get_blueprint_chapters(outline_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    blueprint = outline_data.get('blueprint') if isinstance(outline_data, dict) else {}
    chapters = blueprint.get('chapters') if isinstance(blueprint, dict) else []
    return chapters if isinstance(chapters, list) else []


def _planner_blueprint_to_display(generated_outline_data: Dict[str, Any], topic: str) -> Optional[Dict[str, Any]]:
    """把 PlannerAgent 真实生成的大纲(章节含 title/teaching_goal/key_points/core_concepts…)
    转换成前端蓝图展示所需的结构(title 带“第N章”、summary、objectives、estimated_hours、resources)。
    返回 None 表示没有可用的真实章节（此时保留原骨架）。"""
    blueprint = generated_outline_data.get('blueprint') if isinstance(generated_outline_data, dict) else None
    if not isinstance(blueprint, dict):
        return None
    gen_chapters = blueprint.get('chapters')
    if not isinstance(gen_chapters, list) or not gen_chapters:
        return None

    def _hours(ch):
        dur = ch.get('duration')
        try:
            mins = float(re.sub(r'[^\d.]', '', str(dur)))
            if mins > 0:
                return max(0.5, round(mins / 60.0, 1))
        except Exception:
            pass
        return 1.5

    display = []
    for i, ch in enumerate(gen_chapters, start=1):
        if not isinstance(ch, dict):
            continue
        raw_title = str(ch.get('title') or '').strip() or f'第{i}章'
        title = raw_title if raw_title.startswith('第') else f'第{i}章 {raw_title}'
        objectives = [str(x).strip() for x in (ch.get('key_points') or ch.get('core_concepts') or []) if str(x).strip()]
        if not objectives:
            goal = str(ch.get('teaching_goal') or '').strip()
            objectives = [goal] if goal else [f'掌握本章关于“{topic}”的核心内容']
        summary = str(ch.get('teaching_goal') or ch.get('curiosity_hook') or '').strip() or f'围绕“{topic}”本章要点展开讲解与练习。'
        display.append({
            'id': f'chapter_{i}',
            'number': i,
            'title': title,
            'summary': summary,
            'teaching_goal': str(ch.get('teaching_goal') or '').strip(),
            'objectives': objectives[:4],
            'estimated_hours': _hours(ch),
            'resources': ['doc', 'ppt', 'quiz'],
            'core_concepts': ch.get('core_concepts') or [],
            'key_points': ch.get('key_points') or [],
            'curiosity_hook': str(ch.get('curiosity_hook') or '').strip(),
        })
    if not display:
        return None

    objectives = [str(o).strip() for o in (blueprint.get('objectives') or []) if str(o).strip()][:5]
    return {
        'chapters': display,
        'chapter_count': len(display),
        'estimated_hours': round(sum(c['estimated_hours'] for c in display), 1),
        'objectives': objectives,
    }


def _slide(
    layout: str,
    title: str,
    teaching_goal: str,
    bullets: List[str],
    speaker_notes: str,
    *,
    slide_type: str = 'concept',
    teaching_task: str = '',
    theme: str = 'academic_light',
    visual_blocks: Optional[List[Dict[str, Any]]] = None,
    teacher_action: str = '',
    student_interaction: str = '',
    chapter_id: str = '',
) -> Dict[str, Any]:
    interaction = student_interaction or '请学习者用自己的话复述本页最关键的一句话。'
    return {
        'type': slide_type,
        'layout': layout,
        'theme': theme,
        'title': title,
        'teaching_goal': teaching_goal,
        'teaching_task': teaching_task or '围绕本页核心问题进行教学解释，并给出可落地的例子或判断方法。',
        'bullets': bullets[:5],
        'visual_blocks': visual_blocks or _default_visual_blocks(layout, title, bullets),
        'speaker_notes': speaker_notes,
        'visual_hint': '用结构化版式把定义、机制、例子和结论拆开呈现。',
        'teacher_action': teacher_action or '先提出问题，再解释概念，最后用例子落地。',
        'student_interaction': interaction,
        'interaction_hint': interaction,
        'animation_ref': '',
        'export_mode': 'static',
        'chapter_id': chapter_id,
    }


def _extract_first_code_block(text: str) -> str:
    """从 Markdown 文本里抽第一个 ``` 代码块的内容；没有代码块则返回空串。"""
    if not text:
        return ''
    m = re.search(r'```[a-zA-Z0-9_+\-]*\n?([\s\S]*?)```', text)
    return m.group(1).strip() if m else ''


def _detect_code_language(text: str) -> str:
    """从 ``` 语言标记粗判代码语言，判不出返回空串。"""
    if not text:
        return ''
    m = re.search(r'```([a-zA-Z0-9_+\-]+)', text)
    if m:
        lang = m.group(1).strip().lower()
        if lang and lang not in ('text', 'plaintext', 'plain'):
            return lang
    return ''


def _generate_example_code(topic: str) -> Optional[Dict[str, Any]]:
    """根据主题生成示例代码块"""
    compact = re.sub(r'\s+', '', str(topic or ''))
    
    code_samples = {
        'python': {
            'pattern': lambda t: 'Python' in t or 'python' in t.lower(),
            'language': 'python',
            'code': '''def calculate_average(numbers):
    """计算列表中数字的平均值"""
    if not numbers:
        return 0
    return sum(numbers) / len(numbers)

# 使用示例
scores = [85, 92, 78, 90, 88]
avg = calculate_average(scores)
print(f"平均分: {avg:.2f}")''',
            'explanation': '这段代码演示了如何定义函数、处理边界条件和格式化输出。',
        },
        'datastructure': {
            'pattern': lambda t: '数据结构' in t or 'datastructure' in t.lower() or '栈' in t or 'queue' in t.lower() or 'stack' in t.lower(),
            'language': 'python',
            'code': '''class Stack:
    """栈的实现 - 后进先出(LIFO)"""
    def __init__(self):
        self.items = []
    
    def push(self, item):
        self.items.append(item)
    
    def pop(self):
        if not self.is_empty():
            return self.items.pop()
        return None
    
    def is_empty(self):
        return len(self.items) == 0

# 使用示例
stack = Stack()
stack.push(1)
stack.push(2)
print(stack.pop())  # 输出: 2''',
            'explanation': '这段代码演示了栈数据结构的基本操作。',
        },
        'database': {
            'pattern': lambda t: '数据库' in t or 'database' in t.lower() or 'SQL' in t,
            'language': 'sql',
            'code': '''-- 创建学生表
CREATE TABLE students (
    id INT PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(100) NOT NULL,
    age INT,
    grade VARCHAR(20)
);

-- 查询某个班级的所有学生
SELECT name, age 
FROM students 
WHERE grade = '三年级' 
ORDER BY age DESC;''',
            'explanation': '这段SQL展示了表的创建和查询操作。',
        },
        'network': {
            'pattern': lambda t: '计算机网络' in t or 'network' in t.lower() or 'socket' in t.lower(),
            'language': 'python',
            'code': '''import socket

# 创建TCP客户端
def tcp_client():
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(('localhost', 8080))
    client.sendall(b'Hello, Server!')
    response = client.recv(1024)
    print(f"收到响应: {response.decode()}")
    client.close()

tcp_client()''',
            'explanation': '这段代码演示了TCP客户端的基本实现。',
        },
        'gradient': {
            'pattern': lambda t: '梯度下降' in t or 'gradient' in t.lower(),
            'language': 'python',
            'code': '''import numpy as np

def gradient_descent(X, y, learning_rate=0.01, iterations=1000):
    """梯度下降实现"""
    m = len(y)
    theta = np.zeros(X.shape[1])
    
    for _ in range(iterations):
        predictions = X @ theta
        error = predictions - y
        gradient = (2/m) * (X.T @ error)
        theta -= learning_rate * gradient
    
    return theta

# 使用示例
X = np.array([[1, 1], [1, 2], [1, 3]])
y = np.array([2, 3, 4])
theta = gradient_descent(X, y)
print(f"参数: {theta}")''',
            'explanation': '这段代码实现了线性回归的梯度下降算法。',
        },
    }
    
    for key, sample in code_samples.items():
        if sample['pattern'](compact):
            return {
                'language': sample['language'],
                'code': sample['code'],
                'explanation': sample['explanation'],
            }
    
    return None


def _generate_animation_content(topic: str) -> Optional[Dict[str, Any]]:
    """根据主题生成动画内容"""
    compact = re.sub(r'\s+', '', str(topic or ''))
    
    animations = {
        'gradient': {
            'pattern': lambda t: '梯度下降' in t or 'gradient' in t.lower(),
            'animation_type': 'gradient_descent',
            'animation_code': '''<!DOCTYPE html>
<html><head>
<style>
  body { font-family: Arial; display: flex; flex-direction: column; align-items: center; padding: 20px; }
  .chart { width: 400px; height: 300px; position: relative; border: 1px solid #ccc; background: #fafafa; }
  .point { position: absolute; width: 20px; height: 20px; background: #38bdf8; border-radius: 50%; transform: translate(-50%, -50%); transition: all 0.8s ease; box-shadow: 0 2px 8px rgba(0,0,0,0.2); }
  .axis { position: absolute; background: #333; }
  .x-axis { bottom: 0; left: 0; right: 0; height: 2px; }
  .y-axis { left: 0; top: 0; bottom: 0; width: 2px; }
  .label { position: absolute; font-size: 12px; color: #666; }
  .x-label { bottom: -20px; left: 50%; }
  .y-label { top: 50%; left: -30px; transform: rotate(-90deg); }
</style>
</head>
<body>
  <h3>梯度下降可视化</h3>
  <div class="chart">
    <div class="x-axis"></div>
    <div class="y-axis"></div>
    <svg class="curve" viewBox="0 0 400 300" style="position:absolute;bottom:0;left:0;width:100%;height:100%;">
      <path d="M0,280 Q100,180 200,80 T400,100" fill="none" stroke="#12568a" stroke-width="3"/>
    </svg>
    <div class="point" id="point" style="left: 80px; top: 220px;"></div>
    <span class="label x-label">参数θ</span>
    <span class="label y-label">损失J</span>
  </div>
  <p id="info" style="margin: 15px 0; font-size: 16px;">点击"开始"按钮开始演示</p>
  <button onclick="animate()" style="padding: 10px 24px; font-size: 16px; cursor: pointer;">开始动画</button>
<script>
  const point = document.getElementById('point');
  const positions = [
    {x: 80, y: 220}, {x: 120, y: 180}, {x: 160, y: 140}, 
    {x: 200, y: 80}, {x: 220, y: 70}, {x: 230, y: 68}
  ];
  let idx = 0;
  function animate() {
    if (idx >= positions.length) { idx = 0; }
    point.style.left = positions[idx].x + 'px';
    point.style.top = positions[idx].y + 'px';
    document.getElementById('info').textContent = `迭代 ${idx + 1}: 逐步逼近最小值`;
    idx++;
    if (idx <= positions.length) setTimeout(animate, 800);
  }
</script>
</body></html>''',
            'usage_note': '演示梯度下降算法如何逐步逼近函数最小值，观察参数θ和损失J的变化。',
        },
        'datastructure': {
            'pattern': lambda t: '栈' in t or 'stack' in t.lower() or '队列' in t or 'queue' in t.lower(),
            'animation_type': 'data_structure',
            'animation_code': '''<!DOCTYPE html>
<html><head>
<style>
  body { font-family: Arial; display: flex; flex-direction: column; align-items: center; padding: 20px; }
  .container { display: flex; gap: 40px; align-items: flex-start; }
  .stack-view { display: flex; flex-direction: column; align-items: center; gap: 8px; }
  .stack-label { font-weight: bold; margin-bottom: 10px; }
  .stack-items { display: flex; flex-direction: column-reverse; gap: 4px; min-height: 200px; justify-content: flex-start; }
  .item { width: 80px; height: 36px; background: #12568a; color: white; display: flex; align-items: center; justify-content: center; border-radius: 6px; transition: all 0.4s ease; }
  .controls { margin-top: 20px; display: flex; gap: 10px; }
  button { padding: 8px 16px; font-size: 14px; cursor: pointer; }
  #info { margin-top: 15px; font-size: 16px; }
</style>
</head>
<body>
  <h3>栈操作演示 (LIFO)</h3>
  <div class="container">
    <div class="stack-view">
      <div class="stack-label">栈 Stack</div>
      <div class="stack-items" id="stack"></div>
    </div>
  </div>
  <div class="controls">
    <button onclick="push()">入栈 Push</button>
    <button onclick="pop()">出栈 Pop</button>
    <button onclick="clearStack()">清空</button>
  </div>
  <p id="info">栈为空 - 可以开始入栈操作</p>
<script>
  const stack = [];
  const container = document.getElementById('stack');
  let counter = 1;
  function push() {
    const item = document.createElement('div');
    item.className = 'item';
    item.textContent = `数据${counter++}`;
    item.style.opacity = '0';
    item.style.transform = 'translateY(-20px)';
    container.appendChild(item);
    stack.push(item);
    setTimeout(() => { item.style.opacity = '1'; item.style.transform = 'translateY(0)'; }, 10);
    document.getElementById('info').textContent = `入栈成功: 栈中有 ${stack.length} 个元素`;
  }
  function pop() {
    if (stack.length > 0) {
      const removed = stack.pop();
      removed.style.opacity = '0';
      removed.style.transform = 'translateX(50px)';
      setTimeout(() => removed.remove(), 400);
      document.getElementById('info').textContent = `出栈成功: ${stack.length === 0 ? '栈为空' : '栈中有 ' + stack.length + ' 个元素'}`;
    } else {
      document.getElementById('info').textContent = '栈为空！无法出栈';
    }
  }
  function clearStack() {
    stack.forEach(item => item.remove());
    stack.length = 0;
    counter = 1;
    document.getElementById('info').textContent = '栈已清空';
  }
</script>
</body></html>''',
            'usage_note': '演示栈的入栈(Push)和出栈(Pop)操作，理解后进先出(LIFO)的特点。',
        },
    }
    
    for key, anim in animations.items():
        pattern = getattr(anim, 'pattern', lambda t: anim['pattern'](t) if 'pattern' in anim else False)
        if callable(pattern) and pattern(compact):
            return {
                'animation_type': anim['animation_type'],
                'animation_code': anim['animation_code'],
                'usage_note': anim['usage_note'],
            }
        if 'pattern' in anim and anim['pattern'](compact):
            return {
                'animation_type': anim['animation_type'],
                'animation_code': anim['animation_code'],
                'usage_note': anim['usage_note'],
            }

    # 不再返回"任何主题都硬塞的分步递进"通用模板动画——那种固定套路动画会让很多页
    # 莫名其妙冒出雷同的"分步演示"。只有上面 pattern 命中的主题（有真正贴合概念的手写动画）
    # 才返回动画；其余主题一律 None，由 AI 在骨架里判断是否需要动画、并真生成贴合内容的动画。
    return None


def _smart_build_slide_deck(topic: str, outline_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """智能构建PPT，根据主题决定需要哪些页面"""
    chapters = _get_blueprint_chapters(outline_data)
    profile = _topic_teaching_profile(topic, outline_data)
    blueprint = (outline_data.get('blueprint') or {}) if isinstance(outline_data, dict) else {}
    
    slides = []
    
    # 1. 封面页 - 必须有
    objectives = _as_text_list(blueprint.get('objectives'), limit=4) or profile.get('objectives', [])
    if not objectives:
        objectives = [f'建立"{topic}"的整体框架', '掌握核心概念与基本方法', '完成一次小练习巩固理解']
    
    slides.append(_slide(
        'cover',
        topic,
        '先说明这节课要解决什么问题，以及学完以后能做什么。',
        objectives[:3],
        f'开场不要直接堆定义。先告诉学习者：今天学习"{topic}"，是为了解决一个真实理解问题；然后说明本课会按"问题、概念、机制、例子、自测"的顺序展开。',
        slide_type='cover',
        teaching_task='建立本课目标，让学生知道这节课要解决什么问题、学完后能完成什么判断或解释。',
        teacher_action='用一个生活化或学科内的真实问题开场，说明为什么这个知识点值得学。',
        student_interaction='先让学习者说出自己听到这个主题时最困惑的一点。',
    ))
    
    # 2. 检查是否需要议程页（内容多且复杂时才需要）
    needs_agenda = bool(chapters) or len(profile) > 5
    if needs_agenda:
        slides.append(_slide(
            'agenda',
            '本课学习路线',
            '让学习者知道接下来不是零散知识，而是一条完整理解路径。',
            ['为什么需要它', '它到底是什么', '它如何工作', '用例子检验理解', '避开常见误区'],
            f'这一页相当于学习地图。讲清楚"{topic}"时，要先让学生知道每一步回答的问题：为什么、是什么、怎么做、怎么判断。',
            slide_type='agenda',
            visual_blocks=[
                {'kind': 'step', 'label': '动机', 'text': '先回答为什么要学'},
                {'kind': 'step', 'label': '定义', 'text': '再给出准确概念'},
                {'kind': 'step', 'label': '机制', 'text': '拆开内部流程'},
                {'kind': 'step', 'label': '应用', 'text': '用例子判断是否理解'},
                {'kind': 'step', 'label': '复盘', 'text': '总结误区和自测'},
            ],
        ))
    
    # 3. 动机/为什么需要它
    if profile.get('why_bullets'):
        slides.append(_slide(
            'two_column',
            f'为什么需要"{topic}"',
            '从问题出发，解释这个知识点解决的核心矛盾。',
            profile.get('why_bullets'),
            f'这一页要把抽象主题落到问题上：如果没有"{topic}"，会遇到什么麻烦？有了它以后，哪些关系被组织起来，哪些判断变得清楚？',
            teaching_task='解释学习动机，说明这个知识点解决了什么实际问题或理解障碍。',
            visual_blocks=profile.get('why_visual_blocks') or [
                {'kind': 'bullet_card', 'label': '没有它', 'items': ['概念容易碎片化', '步骤之间缺少因果', '遇到题目只能死记']},
                {'kind': 'bullet_card', 'label': '学会它', 'items': ['能说清核心作用', '能解释工作过程', '能迁移到例题或场景']},
            ],
        ))
    
    # 4. 动画页（如果主题需要）
    animation = _generate_animation_content(topic)
    if animation:
        slides.append(_slide(
            'animation_embed',
            f'动态演示：{topic}',
            '通过动画直观理解工作过程。',
            [animation['usage_note']],
            f'这一页用动画演示"{topic}"的工作过程。动画能帮助学生直观看到抽象概念的动态变化，加深理解。',
            slide_type='concept',
            visual_blocks=[
                {
                    'kind': 'animation',
                    'label': animation['animation_type'],
                    'animation_code': animation['animation_code'],
                    'usage_note': animation['usage_note'],
                },
            ],
        ))
    
    # 5. 核心概念/定义页
    if profile.get('concept_bullets'):
        slides.append(_slide(
            'concept_map',
            f'一句话讲清"{topic}"',
            '给出可复述的核心定义，并拆成几个组成部分。',
            profile.get('concept_bullets'),
            f'这里要给出一个清楚、短句化的定义。讲完以后要求学生能回答：它处理什么对象？起什么作用？最后带来什么结果？',
            visual_blocks=profile.get('concept_visual_blocks') or [
                {'kind': 'concept_node', 'label': '对象', 'text': '它作用在哪些对象或问题上'},
                {'kind': 'concept_node', 'label': '作用', 'text': '它负责组织、连接、计算或判断什么'},
                {'kind': 'concept_node', 'label': '结果', 'text': '它让问题变得更清楚或更高效'},
                {'kind': 'concept_node', 'label': '边界', 'text': '它不能解决什么，容易和什么混淆'},
            ],
        ))
    
    # 6. 机制/流程页
    if profile.get('process_bullets'):
        slides.append(_slide(
            'process_flow',
            f'"{topic}"是怎么起作用的',
            '把内部机制拆成可跟随的步骤，而不是停留在概念名词。',
            profile.get('process_bullets'),
            f'这一页不要只讲结论，要按步骤讲。即使"{topic}"本身不是严格流程，也要把理解过程拆成"条件、规则、执行、结果"四步。',
            visual_blocks=profile.get('process_visual_blocks') or [
                {'kind': 'step', 'label': '条件', 'text': '先看问题给了什么信息'},
                {'kind': 'step', 'label': '规则', 'text': '找到适用的概念或原则'},
                {'kind': 'step', 'label': '执行', 'text': '按顺序完成分析或操作'},
                {'kind': 'step', 'label': '检查', 'text': '判断结果是否符合定义和场景'},
            ],
        ))
    
    # 7. 代码页（如果主题需要）
    code = _generate_example_code(topic)
    if code:
        slides.append(_slide(
            'two_column',
            f'代码示例',
            '通过代码实现加深对概念的理解。',
            [code['explanation']],
            f'这一页展示"{topic}"的代码实现。讲解时要把代码和之前讲的概念对应起来，说明每一部分实现了什么功能。',
            slide_type='practice',
            visual_blocks=[
                {
                    'kind': 'code',
                    'label': f'{code["language"]}代码',
                    'language': code['language'],
                    'code': code['code'],
                },
                {
                    'kind': 'bullet_card',
                    'label': '代码说明',
                    'items': [
                        code['explanation'],
                        '理解代码中的关键变量和逻辑',
                        '尝试修改参数观察变化',
                    ],
                },
            ],
        ))
    
    # 8. 案例页
    if profile.get('case_bullets'):
        slides.append(_slide(
            'case_study',
            f'用一个例子理解"{topic}"',
            '通过具体情境让抽象概念落地。',
            profile.get('case_bullets'),
            f'案例页要用来检验学生是不是真的听懂。先抛出情境，不急着给答案；再带学生找对象、找条件、找规则，最后给结论。',
            slide_type='case',
            visual_blocks=profile.get('case_visual_blocks') or [
                {'kind': 'case', 'label': '案例情境', 'text': f'遇到一个需要判断或解释"{topic}"的具体问题'},
                {'kind': 'bullet_card', 'label': '分析路径', 'items': ['找出关键对象', '匹配定义中的作用', '按照流程推出结果']},
                {'kind': 'bullet_card', 'label': '结论表达', 'items': ['先说判断', '再说依据', '最后说明边界']},
            ],
        ))
    
    # 9. 误区对比页（只有存在易混淆概念时才需要）
    if profile.get('comparison_bullets'):
        slides.append(_slide(
            'comparison',
            '容易混淆的地方',
            '提前拆掉误区，避免学生只记住表面词语。',
            profile.get('comparison_bullets'),
            f'这一页专门讲误区。很多学生觉得"{topic}"难，不是因为定义长，而是把它和相邻概念混在一起，或者不知道什么时候该用。',
            visual_blocks=profile.get('comparison_visual_blocks') or [
                {'kind': 'compare_column', 'label': '正确理解', 'items': ['先看对象和场景', '关注作用和结果', '能解释为什么']},
                {'kind': 'compare_column', 'label': '常见误区', 'items': ['只背关键词', '忽略适用条件', '把相近概念混用']},
            ],
        ))
    
    # 10. 自测页
    if profile.get('quiz_bullets'):
        slides.append(_slide(
            'quiz_check',
            '随堂检查：你真的听懂了吗',
            '用三道小问题检查定义、机制和应用。',
            profile.get('quiz_bullets'),
            '自测不要只问记忆题，要覆盖定义、机制和应用。学生答不上来时，就回到前面的"对象、作用、结果"和流程页。',
            slide_type='practice',
            visual_blocks=profile.get('quiz_visual_blocks') or [
                {'kind': 'question', 'label': '定义题', 'text': f'用一句话解释"{topic}"'},
                {'kind': 'question', 'label': '机制题', 'text': '说出它起作用的关键步骤'},
                {'kind': 'question', 'label': '应用题', 'text': '给一个例子并说明判断依据'},
            ],
        ))
    
    # 11. 总结页（只有内容丰富时才单独一页）
    if profile.get('summary_bullets') and len(slides) > 6:
        slides.append(_slide(
            'summary',
            '本课总结',
            '把整节课收束成可以带走的理解框架。',
            profile.get('summary_bullets'),
            f'最后一页要帮助学生形成可复用框架：以后遇到"{topic}"相关问题，先问为什么需要它，再问它是什么，最后问它如何在具体场景中起作用。',
            slide_type='summary',
        ))
    
    # 12. 如果有章节内容，添加章节详情页
    for index, chapter in enumerate(chapters[:2], start=1):
        chapter_title = str(chapter.get('title') or f'第{index}章 核心内容')
        chapter_objectives = _as_text_list(chapter.get('objectives'), limit=3)
        if not chapter_objectives:
            chapter_objectives = [chapter.get('summary') or f'理解"{topic}"中的一个关键知识点']
        
        slides.append(_slide(
            'concept_map' if index == 1 else 'case_study',
            chapter_title,
            str(chapter.get('summary') or '讲清本章关键概念、适用场景和判断方法。'),
            chapter_objectives + [f'把"{chapter_title}"放回"{topic}"的整体框架中理解'],
            f'这一页补充课程蓝图里的章节内容：{chapter_title}。讲解时先说明它回答什么问题，再说明它和"{topic}"整体之间的关系。',
            chapter_id=str(chapter.get('id') or f'chapter_{index}'),
        ))
    
    return slides


def _synth_speaker_notes(topic: str, title: str, bullets: List[str]) -> str:
    """当某页没有讲稿(生成失败/兜底)时，用本页要点合成一段能真正念出来的讲稿，
    而不是"讲解X的核心要点。"这种空话——要点本身就是真实学科内容。"""
    real = [str(b).strip() for b in (bullets or []) if str(b).strip()]
    if real:
        body = ' '.join(real[:4])
        return f'同学们，这一页我们讲“{title}”。{body}'
    return f'同学们，这一页我们讲“{title}”，请结合“{topic}”的具体例子来理解它的核心内容。'


def normalize_slide_deck(payload: Any, topic: str, outline_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    if isinstance(payload, str):
        payload = _extract_json_object_robust(payload)
    
    slides = []
    analysis = None
    deck_decisions = None
    
    # 解析新的输出格式（包含 analysis、deck_decisions、slides）
    if isinstance(payload, dict):
        # 提取分析信息
        analysis = payload.get('analysis')
        deck_decisions = payload.get('deck_decisions')
        
        # 提取幻灯片
        slides = payload.get('slides') or payload.get('slide_deck') or []
    
    elif isinstance(payload, list):
        slides = payload
    
    # 记录分析信息到日志
    if analysis:
        logger.info(f"Topic analysis for '{topic}': subject={analysis.get('subject')}, difficulty={analysis.get('difficulty')}, strategy={analysis.get('teaching_strategy')}")
    
    normalized = []
    if slides:
        for index, slide in enumerate(slides if isinstance(slides, list) else [], start=1):
            if isinstance(slide, str):
                lines = [line.strip() for line in slide.splitlines() if line.strip()]
                title = lines[0] if lines else f'第{index}页'
                bullets = lines[1:4] or ['理解本页核心内容']
                slide = {'title': title, 'bullets': bullets}
            
            if not isinstance(slide, dict):
                continue
            
            title = str(slide.get('title') or slide.get('heading') or f'第{index}页').strip()
            
            # 从 slide['content']['bullets'] 或 slide['bullets'] 获取要点
            content = slide.get('content') or {}
            bullets = _as_text_list(
                content.get('bullets') or slide.get('bullets') or slide.get('points') or slide.get('content'),
                limit=5
            )
            
            # 如果只有一个幻灯片且没有指定布局，默认为封面
            layout_value = slide.get('layout') or slide.get('type')
            if not layout_value and len(slides) == 1:
                layout_value = 'cover'
            
            # 从 slide['content']['visual_blocks'] 或 slide['visual_blocks'] 获取视觉块
            visual_blocks_data = content.get('visual_blocks') or slide.get('visual_blocks')
            normalized_layout = _normalize_slide_layout(layout_value)  # 复用同一个 layout，避免与页面版式错配

            normalized.append({
                'layout': normalized_layout,
                'theme': _normalize_slide_theme(slide.get('theme')),
                'title': title,
                'teaching_goal': str(slide.get('teaching_goal') or content.get('teaching_goal') or '掌握本页核心内容').strip(),
                'teaching_task': str(slide.get('teaching_task') or '').strip(),
                'bullets': bullets,
                'visual_blocks': _normalize_visual_blocks(
                    visual_blocks_data,
                    normalized_layout,
                    title,
                    bullets,
                ),
                'speaker_notes': str(slide.get('speaker_notes') or slide.get('notes') or _synth_speaker_notes(topic, title, bullets)).strip(),
                'teacher_action': str(slide.get('teacher_action') or '').strip(),
                'student_interaction': str(slide.get('student_interaction') or slide.get('interaction') or slide.get('interaction_hint') or '').strip(),
                'chapter_id': str(slide.get('chapter_id') or '').strip(),
            })
    
    # 如果没有幻灯片或数量不足，从智能构建补充
    if len(normalized) < 3:
        smart_slides = _smart_build_slide_deck(topic, outline_data)
        # 保留原始幻灯片，用智能幻灯片补充
        if normalized:
            normalized.extend(smart_slides[:6 - len(normalized)])
        else:
            normalized = smart_slides
    
    return normalized


def build_deck_skeleton_prompt(topic: str, outline_data: Dict[str, Any], standards: Optional[Dict[str, Any]] = None) -> str:
    """第一阶段：生成PPT的教学蓝图。
    不是只要结构——要为每一页规划具体的教学内容、核心解释、例子和学生问题，
    这样第二阶段才能生成真正有深度、真正教会学生的内容。"""
    blueprint = (outline_data.get('blueprint') or {}) if isinstance(outline_data, dict) else {}
    chapters = blueprint.get('chapters') if isinstance(blueprint, dict) else []
    chapters = chapters if isinstance(chapters, list) else []

    chapter_lines = []
    for index, chapter in enumerate(chapters[:6], start=1):
        if not isinstance(chapter, dict):
            continue
        cid = str(chapter.get('id') or f'chapter_{index}')
        desc = chapter.get('teaching_goal') or chapter.get('summary') or ''
        chapter_lines.append(f'- {cid}：{chapter.get("title") or ""}（{desc}）')
    chapters_text = '\n'.join(chapter_lines) or '（无章节信息，围绕主题自行规划）'

    concepts = (standards or {}).get('concepts') or []
    concept_labels = ', '.join(
        str(c.get('label', '')) for c in concepts if isinstance(c, dict) and c.get('label')
    ) or topic

    return f'''你是一名资深大学讲师，请为主题"{topic}"设计一份完整的教学PPT蓝图。

核心知识点：{concept_labels}
课程章节参考：
{chapters_text}

## 任务要求
为这套PPT的每一页写出详细的教学蓝图，不只是标题和版式——要让后续内容生成者看到蓝图就知道这页该讲什么、怎么讲、用什么例子。

## 每页必须包含以下字段

- **layout**: 版式（从 cover/concept_map/two_column/process_flow/case_study/comparison/quiz_check/summary/animation_embed 中选）
- **theme**: 主题（academic_light/tech_blue/chalkboard_dark 三选一）
- **title**: 这页的标题——要具体有吸引力，不是"介绍"或"概述"
- **teaching_goal**: 学生学完这页能做什么（用"能…"开头，具体可衡量）
- **core_explanation**: 【最重要】这页要讲的核心内容——必须包含具体定义/公式/机制，至少3-4句完整的解释，不能只写一个词或一个短语。这是后续内容生成的主要依据。
- **concrete_example**: 一个具体的例子或类比——必须有场景、数字或可视化描述，帮助学生建立直觉。不能写"举一个例子"这类空话。
- **key_question**: 学生在这页最可能问的一个问题（讲稿必须回答它）
- **needs_code**: 布尔值，quiz_check/cover/summary 之外的页面按内容是否适合代码来判断
- **needs_animation**: 布尔值，【默认 false，从严】。绝大多数页面都不需要动画。只有当这一页的核心概念本身有"随时间推进的动态过程/状态逐步变化"、并且用静态图/文字讲不清楚、动画能实质性提升理解时，才设为 true（例如：梯度一步步下降、数据在网络中前向流动、排序过程逐次交换）。像定义、对比、举例、总结、纯概念这类页面一律 false。宁可不加，也不要为了热闹硬加动画。
- **needs_quiz**: 布尔值，通常只有 quiz_check 页为 true
- **one_line**: 用一句话（≤25字）概括本页到底讲了什么，供相邻页了解彼此内容、避免重复。
- **narrative_role**: 本页在整堂课叙事中的角色，从 motivation(引入动机)/definition(核心定义)/mechanism(原理机制)/example(举例应用)/contrast(对比辨析)/consolidation(巩固自测) 中选一个。

## 幻灯片页序要求
1. 第1页必须是 cover（封面），最后1-2页必须是 quiz_check 或 summary
2. 中间页按"动机 → 核心概念 → 工作原理/流程 → 代码示例（若适合）→ 案例/应用 → 对比辨析 → 巩固"组织
3. 总页数 6-9 页
4. 【整堂课是一条单一叙事线】每页解决一个具体的学习问题，相邻页之间逻辑衔接、层层递进；每页只讲属于自己的内容，绝不与其他页重复同一个知识点。

## 示例（以"梯度下降"为例）
封面页示例：
{{"layout":"cover","theme":"tech_blue","title":"梯度下降：让机器学会从错误中进步","teaching_goal":"能说出梯度下降解决什么问题，以及它与普通参数调整有何不同","core_explanation":"梯度下降是一种通过反复微调参数来最小化损失函数的优化算法。它的核心思想是：在当前参数位置计算损失函数对参数的梯度（即斜率），然后沿着梯度的反方向（下降最快的方向）更新参数，重复这个过程直到损失收敛到最小值。这就是神经网络训练的底层机制。","concrete_example":"想象你蒙着眼睛站在一片起伏的山地上，想找到最低点。你每次都用脚感受脚下的坡度，然后朝最陡的下坡方向迈一步。学习率就是你每步迈多大——步子太大会跨过最低点，步子太小走到猴年马月。","key_question":"为什么不能直接解方程求最小值，非要一步步迭代？","needs_code":false,"needs_animation":false,"needs_quiz":false,"one_line":"梯度下降是什么、解决什么问题","narrative_role":"motivation"}}

原理页示例：
{{"layout":"process_flow","theme":"tech_blue","title":"梯度下降四步走：从前向传播到参数更新","teaching_goal":"能按顺序说出梯度下降的四个步骤，并解释每步的作用","core_explanation":"梯度下降的完整流程分四步：①前向传播——把输入数据送入模型，得到预测值；②计算损失——用损失函数（如均方误差）衡量预测值和真实值的差距，得到一个数字；③反向传播——用链式法则计算每个参数对损失的偏导数，即梯度；④参数更新——把每个参数减去学习率乘以该参数的梯度：θ ← θ - α·∂L/∂θ。重复这四步直到损失不再明显下降。","concrete_example":"设损失L=2，参数w当前值=0.5，梯度∂L/∂w=4，学习率α=0.1，则更新后w=0.5-0.1×4=0.1。损失被减小了，参数向正确方向移动了。","key_question":"为什么要用反向传播而不是直接对每个参数做数值微分？","needs_code":true,"needs_animation":true,"needs_quiz":false,"one_line":"梯度下降的四个步骤及各自作用","narrative_role":"mechanism"}}

## 输出格式
必须只输出合法 JSON，不要 Markdown 代码块，不要任何额外说明。
{{"slides":[{{"layout":"...","theme":"...","title":"...","teaching_goal":"...","core_explanation":"...（至少3句）","concrete_example":"...（具体场景）","key_question":"...","needs_code":false,"needs_animation":false,"needs_quiz":false,"one_line":"...","narrative_role":"..."}}]}}'''


def build_slide_content_prompt(
    topic: str,
    skeleton_slide: Dict[str, Any],
    outline_data: Dict[str, Any],
    standards: Optional[Dict[str, Any]] = None,
    readability_prompt: str = '',
    narrative_context: Optional[Dict[str, Any]] = None,
) -> str:
    """第二阶段：根据教学蓝图为每一页PPT生成完整的教学内容（要点、视觉块、讲稿、师生互动）。"""
    layout = skeleton_slide.get('layout') or 'two_column'
    title = skeleton_slide.get('title') or topic
    teaching_goal = skeleton_slide.get('teaching_goal') or ''
    # 优先使用第一阶段生成的富字段，回退到旧字段
    core_explanation = skeleton_slide.get('core_explanation') or skeleton_slide.get('content_brief') or ''
    concrete_example = skeleton_slide.get('concrete_example') or ''
    key_question = skeleton_slide.get('key_question') or ''
    needs_code = bool(skeleton_slide.get('needs_code'))
    needs_animation = bool(skeleton_slide.get('needs_animation'))
    needs_quiz = bool(skeleton_slide.get('needs_quiz'))

    # 构建教学蓝图部分
    blueprint_lines = [
        f'【教学目标】{teaching_goal or "让学生真正理解并能用自己的话解释本页核心内容"}',
    ]
    if core_explanation:
        blueprint_lines.append(f'【核心内容】{core_explanation}')
    if concrete_example:
        blueprint_lines.append(f'【推荐举例】{concrete_example}（讲稿中必须使用这个或类似的例子）')
    if key_question:
        blueprint_lines.append(f'【学生常问】{key_question}（讲稿中必须正面回答这个问题）')
    blueprint_text = '\n'.join(blueprint_lines)

    lines = [
        f'你是一名经验丰富的大学讲师，正在讲授"{topic}"这门课。',
        f'请为这一页PPT（版式：{layout}，标题："{title}"）生成完整的教学内容。',
        '',
        '## 最重要的视角要求（务必遵守）',
        '这一页是【直接给学生看/听的课件】，目的是把知识本身传授给学生——不是给老师看的教案。',
        '- bullets 与 visual_blocks：直接陈述"知识是什么、为什么、怎么用"，就像课本正文那样把内容讲给学生。',
        '- speaker_notes：是老师对着学生开口讲的话，直接讲解知识、带学生理解。',
        '- 【绝对禁止】在 bullets / visual_blocks / speaker_notes 里写教学设计类、面向老师的话，例如'
        '"本页教学目标是…"、"引导学生思考…"、"老师应先…再…"、"让学生讨论…"、"通过本页学生能…"。'
        '这类"怎么教"的话只能出现在 teacher_action / student_interaction 字段里，绝不能进入讲给学生的正文与讲稿。',
        '- 判断标准：一个学生单独看这一页、听这段讲稿，就能学到实实在在的知识，而不是看到一份"上课流程说明"。',
        '',
    ]

    narrative_block = _format_narrative_context(narrative_context or skeleton_slide.get('narrative_context'))
    if narrative_block:
        lines.append(narrative_block)
        lines.append('')

    lines += [
        '## 教学蓝图（你必须忠实实现以下内容）',
        blueprint_text,
        '',
        '## 输出要求',
        '',
        '### bullets（3-5条要点）',
        '每条要点必须是一个完整的知识陈述，讲清楚概念的含义、原因或用法。',
        '禁止只写词语或标题。',
        '- 错误："损失函数" / "反向传播" / "学习率"',
        '- 正确："损失函数衡量当前模型输出与真实标签的误差大小，它是梯度下降优化的出发点"',
        '',
        '### speaker_notes（最重要——完整课堂讲稿）',
        '写一段至少200字的完整讲稿，要求：',
        '① 开场用问题/悬念/场景切入，不要直接念标题',
        '② 把上方蓝图里的核心内容逐步讲清楚——先"是什么"，再"为什么重要"，再"怎么工作"',
        '③ 直接使用蓝图里的例子/类比（不要替换成空话）',
        '④ 正面回答蓝图里的学生常见问题',
        '⑤ 穿插1-2个互动提问（"大家想想，如果…会怎样？"）',
        '⑥ 最后一句话总结本页要点或引出下一页',
        '讲稿要像真人说话，口语自然，不要像论文摘要。',
        '',
        '### teacher_action',
        '老师在这一页做的1-2件具体事（如"在黑板上推导更新公式"、"演示不同学习率的曲线对比"）',
        '',
        '### student_interaction',
        '学生在这一页参与的具体活动（如"回答…问题"、"动手修改代码里的学习率参数"）',
        '',
        '### visual_blocks（1-3个视觉块）',
        '必须与本页内容强相关，辅助学生理解核心概念。',
    ]

    if needs_code:
        lines.append(
            'visual_blocks 中必须包含至少一个代码块：{"kind":"code","label":"示例代码","language":"python（或其他语言）",'
            '"code":"完整可运行的代码（用 \\n 换行），必须有注释说明每一步在做什么"}。'
            '代码要简洁清晰，能运行，有注释，让学生一看就能理解算法逻辑。'
        )
    if needs_animation:
        lines.append(
            'visual_blocks 中必须包含至少一个动画块：{"kind":"animation","label":"...","concept_name":"动画演示的核心概念",'
            '"usage_note":"一两句话说明动画演示了什么、学生应该重点观察哪里"}。'
        )
    if needs_quiz:
        lines.append(
            'visual_blocks 中必须包含 1-2 个自测题，**只能是选择题**（question_type 必须是 "choice"）：'
            '{"kind":"question","question_text":"题干（要检验理解，不是死记硬背）",'
            '"question_type":"choice","choices":[{"label":"A. ...","value":"A"},{"label":"B. ...","value":"B"},'
            '{"label":"C. ...","value":"C"},{"label":"D. ...","value":"D"}],'
            '"correct_answer":"正确选项的value","explanation":"讲清楚为什么这个答案对、其他选项错在哪里"}。'
            '每道题必须有 3-4 个 choices 和 correct_answer。**禁止出简答题、填空题、判断题、问答题**，只出四选一的单选题。'
        )
    if not (needs_code or needs_animation or needs_quiz):
        if layout == 'comparison':
            lines.append('visual_blocks 包含 2 个对比列：{"kind":"compare_column","label":"...","items":["具体差异1","具体差异2",...]}，items 里每条都要写清楚具体的对比内容。')
        elif layout == 'process_flow':
            lines.append('visual_blocks 包含 3-4 个步骤：{"kind":"step","label":"步骤名","text":"这一步的具体操作和作用"}，text 要说清楚每步做什么、为什么。')
        elif layout == 'concept_map':
            lines.append('visual_blocks 包含 3-4 个概念节点：{"kind":"concept_node","label":"概念名","text":"这个概念的核心含义和作用"}，text 不能只写词语，要解释清楚。')
        else:
            lines.append('visual_blocks 使用要点卡片：{"kind":"bullet_card","label":"标题","items":["每条都是完整句子，讲清楚一个知识点"]}。')

    if readability_prompt:
        lines.append(readability_prompt)

    lines.append('')
    lines.append('## 绝对禁止')
    lines.append('- 禁止在 visual_blocks 的 text/content/items 里写"这里可以…"、"此处展示…"、"可以绘制…"、"待填写"等元指令占位语')
    lines.append('- 禁止写"展示一个流程图"、"可以添加动画"——必须直接写真实教学内容')
    lines.append('- 每个 visual_block 的 text 必须是完整的教学语句（不少于15字），不能只是词语或标题')
    lines.append('')
    lines.append('必须只输出合法 JSON，不要 Markdown 代码块，不要任何额外说明。')
    lines.append(
        '输出格式：{"bullets":["完整句子1","完整句子2",...],"visual_blocks":[{...}],"speaker_notes":"完整讲稿，200字以上",'
        '"teacher_action":"老师的具体操作","student_interaction":"学生的具体活动"}'
    )

    return '\n'.join(lines)


def build_deck_digest(slides: List[Dict[str, Any]]) -> str:
    """把整册草稿压成"每页一行"的浓缩视图，供连贯层体检（小输入）。
    每行含：页号 | 标题 | 前几条要点 | 深度信号(讲稿字数/有无代码/有无数字)。"""
    lines = []
    for i, s in enumerate(slides):
        if not isinstance(s, dict):
            continue
        title = str(s.get('title') or '').strip()
        bullets = [str(b).strip() for b in (s.get('bullets') or []) if str(b).strip()][:4]
        notes = str(s.get('speaker_notes') or '')
        blocks = s.get('visual_blocks') or []
        has_code = any(isinstance(b, dict) and b.get('kind') == 'code' for b in blocks)
        has_digit = any(ch.isdigit() for ch in notes)
        signal = f'讲稿{len(notes)}字/{"有码" if has_code else "无码"}/{"有数" if has_digit else "无数"}'
        bullets_text = ' ; '.join(bullets) if bullets else '(无要点)'
        lines.append(f'#{i} | {title} | 要点: {bullets_text} | 深度: {signal}')
    return '\n'.join(lines)


def build_deck_revision_prompt(topic: str, digest: str) -> str:
    """连贯层体检 prompt：只找"跨页重复"和"叙事断裂"，只点名、不改写。"""
    return f'''你是一名PPT教学总编。下面是"{topic}"整套课件每页的浓缩视图（页号从0开始）：

{digest}

## 任务
找出确实存在的问题页并点名（不要改写内容）：
1. duplicate_of：某页的要点与前面某页高度重复（讲了同一个知识点）。
2. broken_flow：某页与前一页衔接突兀、跳跃、缺少过渡。
3. factual_error：某页有【硬性事实错误】——定义写错、公式或结论错误、概念张冠李戴、明显违背学科共识（会让学生记住错误知识）。

## 严格约束
- 只能标注上面真实出现过的页号（0 到 {max(0, len(digest.splitlines()) - 1)}）。
- issue 只能是 "duplicate_of"、"broken_flow"、"factual_error" 三种之一。
- 【factual_error 判定从严】：只有一句话本身是错的、会误导学生才算；"措辞不够严谨/可以更完整/过于绝对"一律不算，不要报。
- instruction 要具体：重复就说"与第X页重复了'…'，本页应改为侧重…"；断裂就说"开头补一句从'…'过渡到'…'"；
  事实错误就说"'…'是错的，正确应为'…'，请据此改写本页"。
- 如果整套课件没有明显问题，返回空列表。宁缺毋滥。

## 输出
必须只输出合法 JSON，不要 Markdown 代码块：
{{"revisions":[{{"index":页号(整数),"issues":["duplicate_of"或"broken_flow"或"factual_error"],"instruction":"具体怎么改"}}]}}'''


def build_slide_revision_prompt(
    topic: str,
    skeleton_slide: Dict[str, Any],
    draft_slide: Dict[str, Any],
    instruction: str,
    prev_digest: str = '',
    next_digest: str = '',
) -> str:
    """定点重写 prompt：在保留本页教学意图的前提下，按体检指令修掉重复/断裂。"""
    title = skeleton_slide.get('title') or draft_slide.get('title') or topic
    draft_bullets = [str(b).strip() for b in (draft_slide.get('bullets') or []) if str(b).strip()]
    draft_notes = str(draft_slide.get('speaker_notes') or '').strip()
    neighbor_lines = []
    if prev_digest:
        neighbor_lines.append(f'上一页：{prev_digest}')
    if next_digest:
        neighbor_lines.append(f'下一页：{next_digest}')
    neighbor_text = '\n'.join(neighbor_lines) or '（无相邻页信息）'
    return f'''你正在修订"{topic}"课件中的一页（标题："{title}"）。

## 相邻页（据此消除重复、写好过渡，不要重复相邻页已讲的内容）
{neighbor_text}

## 本页当前草稿
要点：{" ; ".join(draft_bullets) if draft_bullets else "(无)"}
讲稿：{draft_notes[:300]}

## 修订要求
{instruction}

在保持本页原有教学主题不变的前提下修订：**若修订要求指出了事实错误，必须先把它改正确**；改掉与相邻页重复的内容、补好过渡衔接、保持或提升讲稿深度（讲稿不得比原来更短、不得写空话）。

必须只输出合法 JSON，不要 Markdown 代码块，格式与原页一致：
{{"bullets":["完整句子",...],"visual_blocks":[{{...}}],"speaker_notes":"完整讲稿，不少于原稿长度","teacher_action":"...","student_interaction":"..."}}'''


def build_teaching_lecture_prompt(topic: str, outline_data: Dict[str, Any], standards: Optional[Dict[str, Any]] = None) -> str:
    """生成完整的课堂教学讲义（用于讲义先行法的第一步）。

    思路：LLM 生成连贯长文本的质量远高于生成 JSON 结构体，
    因此先让模型把教学内容写成自然讲稿，再在第二步把讲稿拆成 PPT 格式。
    """
    blueprint = (outline_data.get('blueprint') or {}) if isinstance(outline_data, dict) else {}
    chapters = blueprint.get('chapters') if isinstance(blueprint, dict) else []
    chapters = chapters if isinstance(chapters, list) else []
    chapter_titles = [str(c.get('title') or '') for c in chapters if isinstance(c, dict) and c.get('title')]
    chapters_hint = '、'.join(chapter_titles[:6]) if chapter_titles else ''

    concepts = (standards or {}).get('concepts') or []
    concept_labels = ', '.join(str(c.get('label', '')) for c in concepts if isinstance(c, dict) and c.get('label')) or topic

    chapters_section = f'\n课程涵盖的模块：{chapters_hint}' if chapters_hint else ''
    return f'''你是一名经验丰富的大学讲师，请为"{topic}"写一篇完整的课堂教学讲义。
核心知识点：{concept_labels}{chapters_section}

## 讲义结构要求（按顺序写，不要省略任何一节）

### 一、开场导入（约300字）
用一个真实问题、反常识的现象或有趣的场景引入话题，激发学生好奇心。
不要开门见山地说"今天我们学习XX"，而是先抛出一个让学生想知道答案的问题。

### 二、核心概念（约600字）
用最清晰的语言解释这个主题的核心定义：
- 它到底是什么（精确定义，有公式就写出来）
- 它和学生已知的事物有什么关系
- 用一个具体类比帮学生建立直觉（必须有，不能略过）

### 三、工作原理与机制（约800字）
深入讲解内部工作过程：
- 分步骤讲清楚是怎么运作的（每步单独说清楚）
- 如果有数学推导，带着学生一步一步推导，不要跳步
- 配合一个具体的数字例子（代入真实数字计算）

### 四、代码实现（约400字）
如果主题适合编程实现，写出一个完整可运行的代码示例：
- 代码要简洁，每行都有注释
- 说明代码每个部分在做什么
- 指出最重要的参数或变量

### 五、实际应用场景（约300字）
列举2-3个真实的应用场景，说明这个知识点在实际中解决什么问题。

### 六、常见误区与易混淆点（约300字）
列出学生最容易犯的3个错误理解，每个误区都给出正确理解。

### 七、课堂总结（约200字）
用3-5句话提炼本节课最核心的要点，最后布置一个思考题让学生回去思考。

## 写作风格要求
- 整篇讲义要像真人在课堂上讲话，口语化、有节奏感
- 多用"大家想想……"、"注意这里……"、"你可能会问……"这样的互动语气
- 不要写成教材摘要，不要堆砌定义，要有解释、有类比、有推导过程

直接输出讲义正文，不要任何额外说明。'''


def build_lecture_to_slides_prompt(topic: str, lecture_text: str, outline_data: Dict[str, Any]) -> str:
    """把完整讲义转换成结构化PPT幻灯片（讲义先行法的第二步）。

    关键约束：每页的 speaker_notes 必须来自讲义原文，
    不允许模型临时编造新内容，这样可以保证 PPT 内容真实有深度。
    """
    return f'''你是一名PPT设计师。以下是"{topic}"的完整课堂教学讲义：

--- 讲义开始 ---
{lecture_text}
--- 讲义结束 ---

## 任务
把这篇讲义的内容分配到 7-9 页 PPT 中，形成结构清晰的教学幻灯片。

## 严格约束（违反则输出无效）
1. **speaker_notes 必须来自讲义原文**：每页的讲稿内容直接从上方讲义中截取或精炼，不允许替换成套话或重新编造。
2. **bullets 必须是讲义中的真实陈述**：不能凭空添加讲义里没有的内容。
3. **不能遗漏讲义的核心内容**：讲义里有的重要知识点，每一个都要出现在某一页里。
4. **禁止生成空洞的 bullets**：不能写"梯度下降的原理"，必须写"梯度下降沿损失函数下降最快的方向更新参数，每次迭代让误差减小"。

## 页面设计规则
- 第1页：cover（封面），标题要吸引人，bullets 写本课3个核心问题
- 第2页（可选）：motivation 页，来自讲义"开场导入"部分
- 中间几页：每页对应讲义的一个章节（核心概念/工作原理/代码/应用/误区）
- 最后1-2页：summary（总结）或 quiz_check（自测题，来自讲义总结部分的思考题）

## 每页必须包含的字段
- layout: cover/concept_map/two_column/process_flow/case_study/comparison/quiz_check/summary/animation_embed 之一
- theme: academic_light/tech_blue/chalkboard_dark 之一
- title: 具体的页面标题（不是"概念介绍"这类空话）
- teaching_goal: 能…（具体可衡量）
- bullets: 3-5条完整句子，每条都是讲义中的真实陈述
- speaker_notes: 从讲义对应章节精炼的讲稿，150字以上，保留讲义的口语化风格
- teacher_action: 这页老师做什么
- student_interaction: 这页学生做什么
- visual_blocks: 1-3个视觉块（见下方说明）
- needs_code: true/false
- needs_animation: true/false
- needs_quiz: true/false

## visual_blocks 规则
- 代码页：{{"kind":"code","label":"示例代码","language":"python","code":"直接来自讲义的代码，加好注释（用\\n换行）"}}
- 流程页：{{"kind":"step","label":"步骤名（如：①初始化参数）","text":"这步实际做了什么，从讲义原文提炼，必须是完整句子"}}
- 概念页：{{"kind":"concept_node","label":"概念名","text":"从讲义中截取的完整解释，15字以上"}}
- 对比页：{{"kind":"compare_column","label":"对比项","items":["具体描述1（完整句子）","具体描述2（完整句子）"]}}
- 自测页：{{"kind":"question","question_text":"题干","question_type":"choice","choices":[{{"label":"A. ...","value":"A"}},...], "correct_answer":"B","explanation":"解析"}}

## 绝对禁止（违反则输出无效）
- 禁止在任何 text/content 字段写"这里可以…"、"此处展示…"、"可以绘制…"、"待填写"等元指令或占位描述
- 禁止写"展示一个XXX图"、"可以添加动画"这类指导语，必须写真实教学内容
- 每个 visual_block 的 text 必须是从讲义截取的实质性教学语句，不少于15个字

必须只输出合法 JSON，不要 Markdown 代码块，不要任何额外说明。
格式：{{"slides":[{{"layout":"...","theme":"...","title":"...","teaching_goal":"...","bullets":["..."],"speaker_notes":"...","teacher_action":"...","student_interaction":"...","visual_blocks":[...],"needs_code":false,"needs_animation":false,"needs_quiz":false}}]}}'''


def build_slide_deck_prompt(topic: str, outline_data: Dict[str, Any]) -> str:
    """生成高质量教学PPT的提示词 - 由AI智能决定PPT结构"""
    blueprint_text = json.dumps(outline_data.get('blueprint') or {}, ensure_ascii=False)
    
    return f"""你是一位资深教育设计师和学科专家。请为主题"{topic}"设计一套真正用于教学的高质量PPT。

## 你的角色
- **教学设计师**：设计有效的学习路径和教学策略
- **学科专家**：保证内容准确、深入、有实例
- **视觉设计师**：创造清晰、美观、易于理解的页面布局

## 工作流程
1. **分析主题**：先判断这个主题属于哪个学科、难度等级、核心概念是什么
2. **规划结构**：根据分析结果决定需要哪些页面，以及它们的逻辑顺序
3. **生成内容**：为每一页设计具体的教学内容

## 核心原则
1. **AI完全主导**：不要按固定模板生成，完全根据主题特性自由设计
2. **教学价值优先**：每一页都必须解决一个明确的学习问题
3. **灵活应变**：简单主题3-5页即可，复杂主题可扩展到10-15页
4. **多模态智能**：自主判断何时使用文字、图表、动画、代码

## 输出格式
直接输出JSON，不要任何额外说明：
```json
{{
    "analysis": {{
        "subject": "学科领域（如数学、计算机科学、物理学）",
        "knowledge_type": "知识类型（概念定义/原理机制/算法实现/案例分析）",
        "difficulty": "难度等级（入门/基础/进阶/高级）",
        "core_concepts": ["核心概念1", "核心概念2", "核心概念3"],
        "teaching_strategy": "你的教学策略：先解决什么问题，为什么这样组织内容"
    }},
    "deck_decisions": {{
        "total_pages": N,
        "page_sequence": ["页面1用途", "页面2用途", "..."],
        "multimedia_decisions": ["哪些页面用动画，为什么", "哪些页面用代码，为什么"]
    }},
    "slides": [
        {{
            "layout": "cover|agenda|concept_map|two_column|process_flow|case_study|comparison|quiz_check|summary|animation_embed|code_demo|custom",
            "theme": "academic_light|tech_blue|chalkboard_dark",
            "title": "吸引人的页面标题",
            "teaching_goal": "学生学完这页能做什么（具体可衡量）",
            "bullets": ["要点1：具体内容，不是空话", "要点2：要有实例支撑"],
            "visual_blocks": [
                {{"kind": "concept_node|step|compare_column|bullet_card|question|code|animation|chart|diagram", "label": "标签", "code": "代码内容", "animation_code": "HTML动画代码", "text": "说明文字", "items": ["列表项"], "chart_type": "图表类型（如line/bar/pie）"}}
            ],
            "speaker_notes": "讲稿：像真实老师一样讲解，要有引导、提问、解释，不只是念标题",
            "teacher_action": "这页老师应该做什么（如演示、提问、板书）",
            "student_interaction": "学生应该做什么（如回答问题、动手练习、小组讨论）"
        }}
    ]
}}
```

## 页面类型参考（按需选用，可组合创新）
- **cover**: 封面页，必须有，包含课程标题、核心目标
- **motivation**: 动机页，激发学习兴趣，用问题引入
- **concept_map**: 概念图，讲解核心定义和组成要素
- **process_flow**: 流程图，讲解工作原理和步骤
- **visualization**: 可视化页，用图表或动画展示抽象概念
- **code_demo**: 代码演示页，展示编程实现
- **case_study**: 案例分析页，用真实例子巩固理解
- **comparison**: 对比页，澄清易混淆概念
- **quiz_check**: 自测页，即时检验学习效果
- **summary**: 总结页，提炼核心要点

## AI智能决策指南
### 判断是否需要动画：
- 需要动画的情况：
  - 有抽象的数值变化（如梯度下降、学习率变化）
  - 有空间位置变化（如排序过程、树的遍历）
  - 有状态转换（如状态机、协议握手）
  - 需要展示过程而非结果
- 不需要动画的情况：
  - 纯概念定义
  - 静态数据展示
  - 简单对比说明

### 判断是否需要代码：
- 需要代码的情况：
  - 涉及编程实现
  - 涉及算法步骤
  - 需要展示具体语法
  - 需要让学生动手实践
- 不需要代码的情况：
  - 纯理论概念
  - 非编程相关主题
  - 入门级概述

### 判断是否需要图表：
- 需要图表的情况：
  - 数据对比
  - 流程展示
  - 关系图
  - 统计数据
- 不需要图表的情况：
  - 纯文字概念
  - 简单列表说明

### 判断是否需要自测题（quiz_check页面）：
- 需要自测题的情况：
  - 讲完核心概念后检验理解
  - 有明确的知识点可以出题
  - 需要巩固学习效果
- 自测题设计要求：
  - 每题必须有完整的题目、选项、正确答案、解析
  - 题目要检验理解，不是死记硬背
  - 解析要讲清楚为什么这个答案是对的
  - 建议在PPT中间或结尾放1-2页自测题

## 高质量内容示例
### 封面页（cover）示例：
{{
    "title": "梯度下降：让损失函数最小化",
    "teaching_goal": "理解梯度下降的核心思想，能说出它解决什么问题",
    "bullets": ["梯度下降是机器学习优化参数的迭代方法", "通过沿着负梯度方向更新参数", "学习率控制每次更新的步长"],
    "speaker_notes": "同学们，大家好！今天我们来学习梯度下降这个重要的优化算法。首先我想先问大家一个问题：如果我们要找到一个函数的最小值，应该往哪个方向走最快呢？对啦，就是负梯度方向！这就是我们今天要讲的梯度下降的核心思想。"
}}

### 动机页（motivation）示例：
{{
    "title": "为什么需要梯度下降？",
    "teaching_goal": "能说出梯度下降解决什么问题，没有它会遇到什么困难",
    "bullets": ["手动调整参数效率低，难收敛", "不知道该往哪个方向调整", "需要一种通用的优化方法"],
    "speaker_notes": "想象一下，你要训练一个线性回归模型预测房价。有100个特征，你要怎么调整这100个权重呢？一个一个试？那要试到猴年马月去了！而且你也不知道该调大还是调小对吧？这就是为什么我们需要梯度下降这种智能的优化方法！"
}}

### 概念页（concept_map）示例：
{{
    "title": "一句话讲清梯度下降",
    "teaching_goal": "能用自己的话解释梯度下降的核心概念",
    "bullets": ["梯度：函数在某点上升最快的方向", "负梯度：函数下降最快的方向", "学习率：每次走的步长大小", "迭代：重复更新直到收敛"],
    "speaker_notes": "好，我们来看梯度下降的核心概念。首先，梯度是什么？梯度就是函数在某个点上升最快的方向，对吧？那我们要找最小值，当然要朝反方向走，也就是负梯度方向！学习率就是控制我们每次迈多大步子，太大容易跳过最优解，太小又走得太慢。"
}}

### 流程页（process_flow）示例：
{{
    "title": "梯度下降四步走",
    "teaching_goal": "能按顺序说出梯度下降的四个步骤",
    "bullets": ["1. 定义损失函数", "2. 计算当前梯度", "3. 更新参数", "4. 检查收敛"],
    "speaker_notes": "好，我们来看梯度下降的具体步骤。第一步，先写出我们要最小化的损失函数。第二步，在当前参数位置计算梯度，看看往哪个方向走损失下降最快。第三步，沿着负梯度方向按学习率更新参数。第四步，看看损失是不是还会不会继续下降，如果变化很小了就可以停止了。"
}}

### 代码页（code_demo）示例：
{{
    "title": "梯度下降的Python实现",
    "teaching_goal": "能看懂梯度下降的代码，能修改学习率参数",
    "bullets": ["初始化参数w和b", "计算梯度", "更新w和b", "循环迭代"],
    "speaker_notes": "好，我们来看梯度下降的Python代码！注意看学习率alpha，这是个很重要的参数哦！如果alpha太大，会怎么样？对，会发散！如果太小呢？收敛就会很慢！所以我们要找一个合适的学习率。"
}}

### 自测页（quiz_check）示例：
{{
    "layout": "quiz_check",
    "title": "来，测测你掌握了吗？",
    "teaching_goal": "能独立完成这三道自测题，检验学习效果",
    "visual_blocks": [
        {{
            "kind": "question",
            "label": "问题1：梯度下降沿什么方向更新参数？",
            "question_text": "梯度下降沿什么方向更新参数？",
            "question_type": "choice",
            "choices": [
                {{"label": "A. 梯度方向", "value": "A"}},
                {{"label": "B. 负梯度方向", "value": "B"}},
                {{"label": "C. 随机方向", "value": "C"}},
                {{"label": "D. 零方向", "value": "D"}}
            ],
            "correct_answer": "B",
            "explanation": "梯度下降的核心思想是沿着损失函数下降最快的方向更新参数，而负梯度方向就是下降最快的方向。"
        }},
        {{
            "kind": "question",
            "label": "问题2：学习率太大有什么问题？",
            "question_text": "学习率太大有什么问题？",
            "question_type": "choice",
            "choices": [
                {{"label": "A. 收敛太慢", "value": "A"}},
                {{"label": "B. 可能发散，无法收敛", "value": "B"}},
                {{"label": "C. 没有问题", "value": "C"}},
                {{"label": "D. 计算太慢", "value": "D"}}
            ],
            "correct_answer": "B",
            "explanation": "学习率太大时，参数更新步长过大，可能会跳过最优解，甚至导致损失函数越来越大，无法收敛。"
        }},
        {{
            "kind": "question",
            "label": "问题3：什么时候停止迭代？",
            "question_text": "什么时候停止迭代？",
            "question_type": "choice",
            "choices": [
                {{"label": "A. 损失函数不再下降或变化很小", "value": "A"}},
                {{"label": "B. 达到最大迭代次数", "value": "B"}},
                {{"label": "C. 梯度接近零", "value": "C"}},
                {{"label": "D. 以上都可以", "value": "D"}}
            ],
            "correct_answer": "D",
            "explanation": "停止迭代的条件包括：损失函数变化很小、达到最大迭代次数、梯度接近零等，实际应用中通常会结合多个条件。"
        }}
    ],
    "speaker_notes": "好，现在我们来做三道小测试，看看大家掌握得怎么样！不要怕答错哦，这是检验我们巩固知识的好机会！做完后我会给大家详细讲解每道题。"
}}

### 总结页（summary）示例：
{{
    "title": "梯度下降要点回顾",
    "teaching_goal": "能说出梯度下降的三个核心要点",
    "bullets": ["沿负梯度方向更新参数", "学习率控制步长", "迭代直到收敛"],
    "speaker_notes": "好，我们来回顾一下今天学的内容。梯度下降就是沿着负梯度方向更新参数，学习率控制每次走多大步子，我们要反复迭代直到损失函数收敛。这就是梯度下降！大家都掌握了吗？"
}}

## 讲稿写作要点
1. **开场要有互动提问
2. **讲解要有生活类比
3. **内容要有具体实例
4. **逻辑要有层层递进
5. **结尾要有总结回顾

## 内容质量检查清单
写完每一页后，请检查：
✅ 教学目标是否具体可衡量？
✅ 内容是否有具体例子，不是空话？
✅ 讲稿是否像真实老师，有引导、提问、解释？
✅ 是否设计了学生参与的活动？
✅ 视觉元素是否合适，能辅助理解？

## 课程参考信息
{blueprint_text}"""


def build_animation_prompt(topic: str, outline_data: Dict[str, Any]) -> str:
    blueprint_text = json.dumps(outline_data.get('blueprint') or {}, ensure_ascii=False)
    return (
        f'请先判断教学主题"{topic}"是否真的需要 H5 动画辅助讲解，再决定是否生成动画。\n'
        "必须只输出合法 JSON，不要 Markdown，不要额外说明。\n"
        '格式：{{"need_animation":true|false,"reason":"...","animations":[{{"chapter_id":"chapter_1","concept_name":"...","animation_type":"css|svg|canvas","html":"...","css":"...","js":"...","usage_note":"..."}}]}}。\n'
        "决策要求：只有在内容存在流程变化、状态迁移、空间结构、因果机制或逐步演示价值时，才把 need_animation 设为 true；如果主题更适合静态讲解、定义辨析、总结归纳或案例讨论，就设为 false，并让 animations 返回空数组。\n"
        "内容要求：如果 need_animation=true，可返回 1-2 个最值得做动画的概念；动画必须真的能在 iframe 中直接运行，不能只输出说明文字。每个动画都要服务于讲解一个明确概念，至少包含可见元素变化、流程演示或交互反馈。\n"
        "安全限制：禁止外链脚本、iframe、fetch、XMLHttpRequest、localStorage、cookie、eval、Function、window.top、window.parent。JS 只允许处理按钮、滑块和画布动画。\n"
        f"课程蓝图：{blueprint_text}"
    )


def build_animation_retry_prompt(topic: str, outline_data: Dict[str, Any], existing: List[Dict[str, Any]]) -> str:
    blueprint_text = json.dumps(outline_data.get('blueprint') or {}, ensure_ascii=False)
    existing_text = json.dumps([
        {
            'chapter_id': item.get('chapter_id'),
            'concept_name': item.get('concept_name'),
            'animation_type': item.get('animation_type'),
            'usage_note': item.get('usage_note'),
        }
        for item in existing
    ], ensure_ascii=False)
    return (
        f'主题"{topic}"的第一版动画生成失败或质量不足，请重新生成。\n'
        f"已有动画信息：{existing_text}\n"
        "必须只输出合法 JSON，不要 Markdown，不要额外说明。\n"
        '格式：{{"animations":[{{"chapter_id":"...","concept_name":"...","animation_type":"css|svg|canvas","html":"...","css":"...","js":"...","usage_note":"..."}}]}}\n'
        "要求：确保每个动画都能在 iframe 中直接运行；不要重复已有概念；不要输出空 html；选择最值得演示的概念制作 1-2 个高质量动画。\n"
        "安全限制：禁止外链脚本、iframe、fetch、XMLHttpRequest、localStorage、cookie、eval、Function、window.top、window.parent。\n"
        f"课程蓝图：{blueprint_text}"
    )


_DANGEROUS_JS_PATTERNS = [
    r'\bfetch\s*\(',
    r'\bXMLHttpRequest\b',
    r'\blocalStorage\b',
    r'\bsessionStorage\b',
    r'\bindexedDB\b',
    r'\beval\s*\(',
    r'\bFunction\s*\(',
    # 注意：setTimeout / setInterval / requestAnimationFrame 是动画必需，且在
    # sandbox="allow-scripts"（无 same-origin）的 iframe 中无害，故不列为危险。
    r'\bdocument\.cookie\b',
    r'\bwindow\.location\b',
    r'\bnavigator\.sendBeacon\b',
    r'\bWebSocket\b',
    r'\bimport\s*\(',
    r'\brequire\s*\(',
]
# 区分大小写：否则 \bFunction\s*\( 会误伤 JS 的 function(){} 关键字，把整段动画代码删掉
_DANGEROUS_JS_RE = re.compile('|'.join(_DANGEROUS_JS_PATTERNS))


def _strip_dangerous_js_from_script_tags(html: str) -> str:
    """移除 <script> 标签中所有危险 JS 调用（逐语句过滤）。"""
    def _clean_script(match: re.Match) -> str:
        content = match.group(1)
        cleaned_lines = []
        for stmt in re.split(r'(?<=;)', content):
            if not _DANGEROUS_JS_RE.search(stmt):
                cleaned_lines.append(stmt)
        return '<script>' + ''.join(cleaned_lines) + '</script>'

    return re.sub(r'<script[^>]*>([\s\S]*?)</script>', _clean_script, html, flags=re.IGNORECASE)


def animation_code_is_safe(code: str) -> bool:
    """检查动画代码是否安全（不含危险 JS 且没有禁止标签）。"""
    if not code:
        return True
    if _DANGEROUS_JS_RE.search(code):
        return False
    try:
        _normalize_animation_html(code)
        return True
    except ValueError:
        return False


def sanitize_animation_code(code: str) -> str:
    """清理动画代码：先移除危险 JS 语句，再校验是否含禁止 HTML 标签。"""
    if not code:
        return ''
    cleaned = _strip_dangerous_js_from_script_tags(code)
    try:
        _normalize_animation_html(cleaned)
        return cleaned
    except ValueError:
        return ''


def _normalize_animation_html(html: str) -> str:
    if not html:
        return ''
    allowed_attrs = {'style', 'class', 'id', 'onclick', 'disabled', 'type', 'name', 'value',
                      'placeholder', 'src', 'alt', 'width', 'height', 'viewBox', 'd', 'fill',
                      'stroke', 'stroke-width', 'cx', 'cy', 'r', 'x', 'y', 'xmlns'}
    dangerous = re.findall(r'<(iframe|object|embed|form|input|select|textarea)[^>]*>', html, re.IGNORECASE)
    if dangerous:
        raise ValueError('Forbidden tags: ' + ', '.join(set(t.lower() for t in dangerous)))
    return html


def normalize_animation_assets(assets: Any, topic: str, outline_data: Dict[str, Any], fallback: bool = True) -> List[Dict[str, Any]]:
    if isinstance(assets, str):
        try:
            parsed = json.loads(assets)
            if isinstance(parsed, dict):
                need = parsed.get('need_animation', False)
                if not need and not parsed.get('animations'):
                    return []
                assets = parsed.get('animations') or []
            elif isinstance(parsed, list):
                assets = parsed
        except Exception:
            assets = []
    
    if not assets:
        if fallback:
            return _generate_default_animations(topic)
        return []
    
    result = []
    for item in (assets if isinstance(assets, list) else []):
        if not isinstance(item, dict):
            continue
        
        concept = str(item.get('concept_name') or '').strip()
        if not concept:
            continue
        
        anim_type = str(item.get('animation_type') or 'css').strip().lower()
        usage = str(item.get('usage_note') or item.get('usage') or '').strip()
        
        try:
            html_content = str(item.get('html') or item.get('content') or '').strip()
            css_content = str(item.get('css') or '').strip()
            js_content = str(item.get('js') or '').strip()
            
            full_html = html_content
            if css_content and '<style' not in html_content.lower():
                full_html = f'<style>{css_content}</style>{full_html}'
            # 关键：把 js 拼进去，否则动画没有脚本、根本动不起来（此前 js 被读取却丢弃）
            if js_content and '<script' not in full_html.lower():
                full_html = f'{full_html}<script>{js_content}</script>'
            if not full_html.lower().startswith('<!doctype'):
                full_html = f'<!doctype html>\n{full_html}'

            # 移除真正危险的 JS（fetch/存储/eval 等），但保留动画所需的定时器/rAF
            full_html = _strip_dangerous_js_from_script_tags(full_html)
            _normalize_animation_html(full_html)
            
            result.append({
                'chapter_id': str(item.get('chapter_id') or '').strip(),
                'concept_name': concept,
                'animation_type': anim_type,
                'animation_code': full_html,
                'usage_note': usage,
                'safe': True,
            })
        except ValueError:
            continue
        except Exception:
            continue
    
    return result


def _generate_default_animations(topic: str) -> List[Dict[str, Any]]:
    """为主题生成默认动画"""
    animation_content = _generate_animation_content(topic)
    if animation_content:
        _code = str(animation_content.get('animation_code') or '')
        # 内容本身多半已带 <!DOCTYPE html>，避免再前置一个造成双 doctype
        if not _code.lstrip().lower().startswith('<!doctype'):
            _code = f'<!doctype html>\n{_code}'
        return [{
            'chapter_id': 'default',
            'concept_name': topic,
            'animation_type': animation_content.get('animation_type', 'css'),
            'animation_code': _code,
            'usage_note': animation_content.get('usage_note', ''),
            'safe': True,
        }]
    return []


def _attach_animation_codes(slides: List[Dict[str, Any]], animations: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把动画生成流程产出的 animation_code/animation_type/usage_note 等字段写回
    对应 slide 的 visual_blocks 中 kind=='animation' 的块（纯数据缝合，
    不改动动画生成本身）。优先按 chapter_id 匹配，未匹配的动画按顺序补齐。

    返回 (slides, unconsumed_animations)：unconsumed_animations 是没有被任何
    slide 的 animation 视觉块消费掉的动画，调用方可据此追加专门的
    animation_embed 页面，避免"判断需要动画"的结果被悄悄丢弃。"""
    if not animations:
        return slides, []

    valid_animations = [a for a in animations if isinstance(a, dict) and a.get('animation_code')]
    if not valid_animations:
        return slides, []

    by_chapter: Dict[str, Dict[str, Any]] = {}
    leftovers: List[Dict[str, Any]] = []
    for anim in valid_animations:
        chapter_id = str(anim.get('chapter_id') or '').strip()
        if chapter_id and chapter_id not in by_chapter:
            by_chapter[chapter_id] = anim
        else:
            leftovers.append(anim)

    for slide in slides:
        if not isinstance(slide, dict):
            continue
        for block in (slide.get('visual_blocks') or []):
            if not isinstance(block, dict) or block.get('kind') != 'animation' or block.get('animation_code'):
                continue
            chapter_id = str(slide.get('chapter_id') or '').strip()
            anim = by_chapter.pop(chapter_id, None) if chapter_id else None
            if anim is None and leftovers:
                anim = leftovers.pop(0)
            if anim is None:
                continue
            block['animation_code'] = anim.get('animation_code', '')
            if anim.get('animation_type'):
                block['animation_type'] = anim.get('animation_type')
            if not block.get('concept_name') and anim.get('concept_name'):
                block['concept_name'] = anim.get('concept_name')
            if not block.get('usage_note') and anim.get('usage_note'):
                block['usage_note'] = anim.get('usage_note')

    unconsumed = list(by_chapter.values()) + leftovers
    return slides, unconsumed


def _build_animation_slide(topic: str, animation: Dict[str, Any]) -> Dict[str, Any]:
    """为一个尚未被任何页面消费的动画，构造一页独立的 animation_embed 幻灯片，
    确保"判断需要动画"流程产出的动画真正出现在最终课件中。"""
    concept_name = str(animation.get('concept_name') or topic).strip() or topic
    usage_note = str(animation.get('usage_note') or '').strip() or f'观察"{concept_name}"的动态演示，理解其变化过程。'

    return {
        'layout': 'animation_embed',
        'theme': 'tech_blue',
        'title': f'动态演示：{concept_name}',
        'teaching_goal': f'通过动画直观理解"{concept_name}"的变化过程。',
        'teaching_task': '播放动画并引导学生观察关键变化。',
        'bullets': [usage_note],
        'visual_blocks': [{
            'kind': 'animation',
            'label': concept_name,
            'concept_name': concept_name,
            'animation_type': str(animation.get('animation_type') or 'css'),
            'animation_code': animation.get('animation_code', ''),
            'usage_note': usage_note,
        }],
        'speaker_notes': f'接下来我们通过一段动画来直观感受"{concept_name}"。{usage_note}希望大家仔细观察每一步的变化。',
        'teacher_action': '播放动画，逐步讲解每一阶段发生的变化。',
        'student_interaction': '观察动画演示，并思考每一步变化背后的原理。',
        'chapter_id': str(animation.get('chapter_id') or '').strip(),
    }


def _quiz_slide_from_block(topic: str, base_title: str, question_block: Dict[str, Any], idx: int, total: int) -> Dict[str, Any]:
    """把单个 question 视觉块包成一页独立的 quiz_check 幻灯片，
    让题干和选项有充足空间、不与其他题挤在一起。"""
    q_text = str(question_block.get('question_text') or question_block.get('text') or '随堂自测').strip()
    suffix = f'（第{idx}题）' if total > 1 else ''
    title = f'{base_title or "随堂自测"}{suffix}'
    return {
        'type': 'quiz',
        'layout': 'quiz_check',
        'theme': 'academic_light',
        'title': title,
        'teaching_goal': '检验本节核心知识点的掌握情况。',
        'teaching_task': '独立作答后再看解析，检查自己的理解是否到位。',
        'bullets': [],
        'visual_blocks': [question_block],
        'speaker_notes': f'请大家先独立思考这道题：{q_text} 想清楚再看答案，重点是理解为什么。',
        'teacher_action': '留时间让学生独立作答，再公布答案并讲解每个选项。',
        'student_interaction': '独立作答，然后对照解析检查自己的思路。',
        'chapter_id': str(question_block.get('chapter_id') or '').strip(),
        'quiz_index': idx,
        'quiz_total': total,
    }


def _is_choice_question(block: Any) -> bool:
    """随堂自测只保留选择题：必须有 kind=question、≥2 个选项、且有正确答案。"""
    return (
        isinstance(block, dict)
        and block.get('kind') == 'question'
        and isinstance(block.get('choices'), list)
        and len([c for c in block.get('choices') if c]) >= 2
        and bool(str(block.get('correct_answer') or '').strip())
    )


def _split_quiz_slides(topic: str, slides: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把含多道题、或题目与讲解内容混在一起的页面拆开：每道题单独成一页。
    随堂自测只保留选择题；简答/填空/判断等非选择题目一律丢弃、不渲染。
    单页仅一道题、且本身就是纯 quiz_check 的，保持不变。"""
    result: List[Dict[str, Any]] = []
    for slide in slides:
        if not isinstance(slide, dict):
            result.append(slide)
            continue
        blocks = slide.get('visual_blocks') or []
        questions = [b for b in blocks if _is_choice_question(b)]
        # others 排除所有 question 块（含被丢弃的非选择题），非选择题不进入任何页面
        others = [b for b in blocks if not (isinstance(b, dict) and b.get('kind') == 'question')]

        if not questions:
            # 没有合格的选择题：丢弃任何非选择题目块，其余内容照常保留；只剩非法题目的纯 quiz 页则丢弃
            if others or (slide.get('bullets') or []) or slide.get('layout') != 'quiz_check':
                content_slide = dict(slide)
                content_slide['visual_blocks'] = others
                result.append(content_slide)
            continue

        is_pure_single_quiz = (
            slide.get('layout') == 'quiz_check' and len(questions) == 1 and not others and not (slide.get('bullets') or [])
        )
        if is_pure_single_quiz:
            # 仅保留合格的选择题，剔除同页可能混入的非选择题目块
            single = dict(slide); single['visual_blocks'] = questions
            result.append(single)
            continue

        # 该页混有讲解内容或多道题 → 先保留去掉题目后的内容页（若还有实质内容），再把每道题单独成页
        has_content = bool(slide.get('bullets')) or bool(others)
        if has_content and slide.get('layout') != 'quiz_check':
            content_slide = dict(slide)
            content_slide['visual_blocks'] = others or _default_visual_blocks(
                slide.get('layout') or 'two_column', slide.get('title') or topic, slide.get('bullets') or []
            )
            result.append(content_slide)

        base_title = str(slide.get('title') or '随堂自测').strip()
        total = len(questions)
        for qi, question in enumerate(questions, start=1):
            result.append(_quiz_slide_from_block(topic, base_title, question, qi, total))
    return result


def _drop_empty_animation_blocks(topic: str, slides: List[Dict[str, Any]]) -> None:
    """由 AI 决定某概念是否需要动画：只保留真正生成出可运行动画代码的动画块；
    没拿到真实动画代码的动画块直接删掉——不再硬塞通用"分步演示"模板（那会让很多页莫名其妙冒出雷同动画）。
    若一页本是 animation_embed 却已没有任何可播放动画，降级为普通概念页，避免空白动画页。原地修改。"""
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        kept = []
        for block in (slide.get('visual_blocks') or []):
            if isinstance(block, dict) and block.get('kind') == 'animation' and not str(block.get('animation_code') or '').strip():
                continue  # 空动画块（没有真实可运行代码）→ 丢弃，不塞模板
            kept.append(block)
        slide['visual_blocks'] = kept
        if slide.get('layout') == 'animation_embed' and not any(
            isinstance(b, dict) and b.get('kind') == 'animation' and str(b.get('animation_code') or '').strip()
            for b in kept
        ):
            slide['layout'] = 'concept_map'  # 没有可播放动画的"动画页"降级为普通概念页


def _split_animation_slides(topic: str, slides: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把嵌在内容页里的动画抽出来，单独成一页 animation_embed 全屏演示。
    已经是 animation_embed 的页、以及没有可播放动画代码的块，保持不变。"""
    result: List[Dict[str, Any]] = []
    for slide in slides:
        if not isinstance(slide, dict) or slide.get('layout') == 'animation_embed':
            result.append(slide)
            continue
        blocks = slide.get('visual_blocks') or []
        anims = [
            b for b in blocks
            if isinstance(b, dict) and b.get('kind') == 'animation' and b.get('animation_code')
        ]
        if not anims:
            result.append(slide)
            continue

        others = [b for b in blocks if b not in anims]
        content_slide = dict(slide)
        content_slide['visual_blocks'] = others or _default_visual_blocks(
            slide.get('layout') or 'two_column', slide.get('title') or topic, slide.get('bullets') or []
        )
        result.append(content_slide)

        for anim in anims:
            result.append(_build_animation_slide(topic, {
                'concept_name': anim.get('concept_name') or slide.get('title') or topic,
                'animation_type': anim.get('animation_type') or 'css',
                'animation_code': anim.get('animation_code', ''),
                'usage_note': anim.get('usage_note') or '',
                'chapter_id': slide.get('chapter_id') or '',
            }))
    return result


def _ensure_skeleton_diversity(topic: str, skeleton: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """安全网：确保骨架中至少有一页设置了 needs_quiz=True，避免 AI 把结尾页设计
    为纯总结页（summary）而导致整份课件没有任何随堂测验题。"""
    if not skeleton:
        return skeleton

    if any(slide.get('needs_quiz') for slide in skeleton):
        return skeleton

    target = None
    for slide in skeleton:
        if slide.get('layout') == 'summary':
            target = slide
            break
    if target is None:
        for slide in reversed(skeleton):
            if slide.get('layout') != 'cover':
                target = slide
                break

    if target is not None:
        target['needs_quiz'] = True
        if target.get('layout') != 'animation_embed':
            target['layout'] = 'quiz_check'
            if not target.get('content_brief'):
                target['content_brief'] = f'设计1-2道随堂测验题，检验学生对"{topic}"核心内容的掌握情况。'

    return skeleton


def _ensure_required_visual_blocks(topic: str, skeleton_slide: Dict[str, Any], visual_blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """确保 needs_code/needs_animation/needs_quiz 标志对应的视觉块真的存在于本页内容中。
    AI 在阶段二有时会忽略提示词中对 visual_blocks 的具体要求（例如只返回 bullet_card），
    导致这些标志最终被 _normalize_visual_blocks 的通用兜底悄悄丢弃；这里作为最后一道补全。"""
    # 限制原有块数，确保下面补充的块不会被 _normalize_visual_blocks 的 [:6] 截断丢弃
    blocks = [b for b in (visual_blocks or []) if isinstance(b, dict)][:4]
    title = str(skeleton_slide.get('title') or topic).strip() or topic

    def _of_kind(kind: str) -> List[Dict[str, Any]]:
        return [b for b in blocks if b.get('kind') == kind or b.get('type') == kind]

    if skeleton_slide.get('needs_code'):
        code_blocks = _of_kind('code')
        if not code_blocks:
            code_info = _generate_example_code(topic)
            if code_info:
                blocks.append({
                    'kind': 'code',
                    'label': f'{title} 代码示例',
                    'language': code_info.get('language', 'python'),
                    'code': code_info.get('code', ''),
                    'text': code_info.get('explanation', ''),
                })
        else:
            # AI 给出了代码块但 code 字段为空，用主题对应的兜底代码补全，
            # 避免页面声称"有代码示例"却没有实际内容。
            for code_block in code_blocks:
                if not str(code_block.get('code') or '').strip():
                    code_info = _generate_example_code(topic)
                    if code_info:
                        code_block['code'] = code_info.get('code', '')
                        code_block.setdefault('language', code_info.get('language', 'python'))
                        if not str(code_block.get('text') or '').strip():
                            code_block['text'] = code_info.get('explanation', '')

    if skeleton_slide.get('needs_animation'):
        anim_blocks = _of_kind('animation')
        if not anim_blocks:
            anim_info = _generate_animation_content(topic)
            if anim_info:
                blocks.append({
                    'kind': 'animation',
                    'label': f'{title} 动态演示',
                    'concept_name': topic,
                    'animation_type': anim_info.get('animation_type', 'css'),
                    'animation_code': anim_info.get('animation_code', ''),
                    'usage_note': anim_info.get('usage_note', ''),
                })
        else:
            # AI 给出了动画说明（concept_name/usage_note）但没有提供可运行的动画代码，
            # 用主题对应的兜底动画代码补全，避免页面声称"有动画"却没有实际内容。
            for anim_block in anim_blocks:
                if not str(anim_block.get('animation_code') or '').strip():
                    anim_info = _generate_animation_content(topic)
                    if anim_info:
                        anim_block['animation_code'] = anim_info.get('animation_code', '')
                        anim_block.setdefault('animation_type', anim_info.get('animation_type', 'css'))
                        if not str(anim_block.get('usage_note') or '').strip():
                            anim_block['usage_note'] = anim_info.get('usage_note', '')
                        if not str(anim_block.get('concept_name') or '').strip():
                            anim_block['concept_name'] = topic

    if skeleton_slide.get('needs_quiz') and not _of_kind('question'):
        blocks.append({
            'kind': 'question',
            'label': f'{title} 自测',
            'question_text': f'请用一两句话说明"{title}"的核心内容。',
            'question_type': 'short_answer',
            'choices': [],
            'correct_answer': '',
            'explanation': str(skeleton_slide.get('teaching_goal') or '').strip() or f'回顾"{title}"的教学目标。',
        })

    return blocks


def _fallback_content_for_skeleton_slide(topic: str, skeleton_slide: Dict[str, Any]) -> Dict[str, Any]:
    """当某一页的详细内容生成失败、且该页骨架来自 AI（没有 _fallback_slide 兜底）时，
    依据 content_brief/teaching_goal/title 以及 needs_code/needs_animation/needs_quiz
    标志生成一份非空兜底内容，避免出现完全空白的页面。"""
    layout = skeleton_slide.get('layout') or 'two_column'
    title = str(skeleton_slide.get('title') or topic).strip() or topic
    teaching_goal = str(skeleton_slide.get('teaching_goal') or '').strip()
    content_brief = str(skeleton_slide.get('content_brief') or '').strip()
    base_text = content_brief or teaching_goal or f'{title}的核心内容'

    bullets = [
        base_text,
        f'结合"{topic}"的实际场景理解"{title}"。',
        f'思考"{title}"与前后知识点之间的联系。',
    ]

    visual_blocks = _ensure_required_visual_blocks(topic, skeleton_slide, [])
    if not visual_blocks:
        visual_blocks = _default_visual_blocks(layout, title, bullets)

    speaker_notes = f'今天我们来看"{title}"。{base_text}希望大家结合实际例子理解这部分内容，也欢迎随时提问。'

    return {
        'bullets': bullets,
        'visual_blocks': visual_blocks,
        'speaker_notes': speaker_notes,
        'teacher_action': f'结合实例讲解"{title}"，并提问检验学生理解。',
        'student_interaction': f'请学生用自己的话复述"{title}"的核心内容。',
    }


def slides_to_markdown(slides: List[Dict[str, Any]], topic: str) -> str:
    lines = [f'# {topic}\n']
    for idx, slide in enumerate(slides, 1):
        layout = slide.get('layout', 'unknown')
        title = slide.get('title', f'第{idx}页')
        lines.append(f'\n## 第{idx}页：{title} [{layout}]\n')
        goal = slide.get('teaching_goal', '')
        if goal:
            lines.append(f'**教学目标**：{goal}\n')
        bullets = slide.get('bullets', [])
        if bullets:
            lines.append('\n' + '\n'.join(f'- {b}' for b in bullets) + '\n')
        notes = slide.get('speaker_notes', '')
        if notes:
            lines.append(f'\n**讲稿**：{notes}\n')
    return '\n'.join(lines)


class ResourceGenerationError(Exception):
    pass


class CourseGenerator:
    def __init__(self, user, outline: Optional[Any] = None, material: Optional[Any] = None):
        self.user = user
        self.outline = outline
        self.material = material
        self.client = XinghuoClient()
        self._profile = None

    @property
    def profile(self):
        if self._profile is None:
            from .models import LearnerProfile
            self._profile, _ = LearnerProfile.objects.get_or_create(user=self.user)
        return self._profile

    def _call_llm(self, prompt: str, temperature: float = 0.7) -> str:
        try:
            response = self.client.chat(prompt, temperature=temperature)
            return response
        except Exception as e:
            logger.error('LLM调用失败: %s', str(e))
            raise ResourceGenerationError(f'AI服务调用失败: {str(e)}')

    def _track_progress(self, phase: str, progress: int, message: str = ''):
        if self.outline:
            self.outline.progress = min(progress, 100)
            self.outline.save(update_fields=['progress'])
        if hasattr(self.outline, 'update_progress'):
            self.outline.update_progress(phase, progress, message)

    def _record_event(self, event_type: str, data: dict):
        from .models import ProfileEvent
        ProfileEvent.objects.create(
            profile=self.profile,
            event_type=event_type,
            event_data=data,
        )

    def generate_full_course(self):
        if not self.outline:
            raise ResourceGenerationError('没有课程大纲')
        
        topic = self.outline.title
        outline_data = self.outline.outline_data or {}
        
        self._track_progress('blueprint', 5, '开始生成课程资源')
        
        self._track_progress('ppt', 15, '生成PPT')
        ppt_raw = self._call_llm(build_slide_deck_prompt(topic, outline_data))
        ppt_slides = normalize_slide_deck(ppt_raw, topic, outline_data)
        ppt_md = slides_to_markdown(ppt_slides, topic)
        
        self._track_progress('animation', 45, '生成动画')
        anim_raw = self._call_llm(build_animation_prompt(topic, outline_data))
        anim_assets = normalize_animation_assets(anim_raw, topic, outline_data)
        
        self._track_progress('quiz', 70, '生成练习题')
        quiz_prompt = build_material_quiz_prompt(topic, outline_data, self.profile)
        quiz_raw = self._call_llm(quiz_prompt)
        quiz_obj = parse_quiz_json(quiz_raw, topic)
        
        self._track_progress('doc', 85, '生成讲义')
        doc_prompt = build_teaching_doc_prompt(topic, outline_data, self.profile)
        doc_md = self._call_llm(doc_prompt)
        
        self._track_progress('finalizing', 95, '整理资源')
        
        resources = {
            'ppt': {
                'title': f'{topic} 课件',
                'preview': ppt_md[:500],
                'slides': ppt_slides,
                'animations': anim_assets,
            },
            'quiz': {
                'title': f'{topic} 练习题',
                'preview': quiz_md_preview(quiz_obj),
                'questions': quiz_obj.get('questions', []),
            },
            'doc': {
                'title': f'{topic} 讲义',
                'preview': doc_md[:500],
                'content': doc_md,
            },
        }
        
        self._track_progress('completed', 100, '生成完成')
        
        self._record_event('course_generated', {
            'topic': topic,
            'resource_counts': {k: 1 for k in resources},
        })
        
        return resources


def build_material_quiz_prompt(topic: str, outline_data: Dict[str, Any], profile: Any) -> str:
    audience_level = getattr(profile, 'knowledge_level', 'beginner')
    preferred_mode = getattr(profile, 'learning_preference', 'visual')
    
    blueprint_text = json.dumps(outline_data.get('blueprint') or {}, ensure_ascii=False)
    return (
        f'请为主题"{topic}"生成3-5道练习题，用于检验学习效果。\n'
        "必须只输出合法 JSON，不要 Markdown，不要额外说明。\n"
        '格式：{{"questions":[{{"text":"题目文本","type":"choice|blank|code","choices":["选项A","选项B","选项C","选项D"],"correct_index":0,"explanation":"解析"}}]}}\n'
        "要求：\n"
        "1. 题目要检验真正的理解，不是背诵\n"
        "2. 覆盖定义、机制、应用三个层面\n"
        '3. 答案解析要解释为什么，而不是只说"正确"\n'
        f"4. 适合受众水平：{audience_level}\n"
        f"5. 学习偏好：{preferred_mode}\n"
        f"课程蓝图：{blueprint_text}"
    )


def parse_quiz_json(raw: str, topic: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and 'questions' in parsed:
            return parsed
        if isinstance(parsed, list):
            return {'questions': parsed}
    except Exception:
        pass
    match = re.search(r'\{[\s\S]*\}', raw)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict) and 'questions' in parsed:
                return parsed
        except Exception:
            pass
    return {'questions': [], 'topic': topic}


def quiz_md_preview(quiz_obj: Dict[str, Any]) -> str:
    questions = quiz_obj.get('questions', [])
    if not questions:
        return '暂无练习题'
    lines = []
    for i, q in enumerate(questions[:3], 1):
        # 兼容两套题目 schema：标准法用 question/options，另一套用 text/choices
        stem = q.get('question') or q.get('text') or q.get('stem') or ''
        opts = q.get('options') or q.get('choices') or []
        lines.append(f'{i}. {stem}')
        for c in opts:
            label = c.get('label') or c.get('text') or c.get('value') if isinstance(c, dict) else c
            lines.append(f'   - {label}')
    return '\n'.join(lines)


def build_teaching_doc_prompt(topic: str, outline_data: Dict[str, Any], profile: Any) -> str:
    audience_level = getattr(profile, 'knowledge_level', 'beginner')
    blueprint_text = json.dumps(outline_data.get('blueprint') or {}, ensure_ascii=False)
    return (
        f'你是一名资深大学讲师，请为主题"{topic}"写一份有深度、能自学的教学讲义（Markdown）。\n\n'
        '## 结构\n'
        '1. 开头用一段"导入"点出这门课要解决什么问题、为什么值得学（不要写"本讲义介绍…"这种套话）。\n'
        '2. 按课程蓝图逐章展开，每一章都要包含：核心概念（精确定义/公式）、直觉类比、'
        '原理或推导（分步不跳步）、代入真实数字的具体例子、常见误区与正确理解、1-2 道检验理解的练习（附答案要点）。\n'
        '3. 结尾"本讲小结"提炼要点 + 一个引导深入的思考题。\n\n'
        '## 硬性要求\n'
        '- 讲解具体、有信息量，禁止空话套话；每个概念真正讲透。\n'
        '- 篇幅充实（建议 1500 字以上），不注水。\n'
        f'- 适合受众水平：{audience_level}。\n'
        '- 直接输出 Markdown 正文，不要额外说明。\n\n'
        f'课程蓝图：{blueprint_text}'
    )


def update_outline_with_resources(outline, resources: Dict[str, Any]):
    if not outline or not resources:
        return
    
    outline_data = outline.outline_data or {}
    if 'resources' not in outline_data:
        outline_data['resources'] = {}
    
    for key, value in resources.items():
        if key == 'ppt' and isinstance(value, dict):
            outline_data['resources']['ppt'] = {
                'title': value.get('title', ''),
                'preview': value.get('preview', ''),
                'slides': value.get('slides', []),
                'animations': value.get('animations', []),
            }
        elif key == 'quiz' and isinstance(value, dict):
            outline_data['resources']['quiz'] = {
                'title': value.get('title', ''),
                'preview': value.get('preview', ''),
                'questions': value.get('questions', []),
            }
        elif key == 'doc' and isinstance(value, dict):
            outline_data['resources']['doc'] = {
                'title': value.get('title', ''),
                'preview': value.get('preview', ''),
                'content': value.get('content', ''),
            }
    
    outline.outline_data = outline_data
    outline.save(update_fields=['outline_data'])


class GenerationManager:
    """资源生成管理器：协调多种类型资源的生成（基于COGENT框架+Instructional Agents）"""
    
    PHASES = ['planning', 'outlining', 'drafting', 'review', 'revision', 'finalizing', 'completed']
    
    def __init__(self, user, topic: str, outline_id: Optional[int] = None, 
                 task: Optional[Any] = None, resource_types: Optional[List[str]] = None,
                 user_profile: Optional[dict] = None, grade_level: str = 'college',
                 enable_reflection: bool = True, quality_threshold: int = 85):
        self.user = user
        self.topic = topic
        self.outline_id = outline_id
        self.task = task
        self.resource_types = resource_types or ['doc', 'ppt', 'quiz']
        if user_profile:
            self.user_profile = user_profile
        else:
            from .agents import get_user_profile_dict
            self.user_profile = get_user_profile_dict(user)
        self.grade_level = grade_level
        self.enable_reflection = enable_reflection
        self.quality_threshold = quality_threshold

        # ── 快速模式开关（本地测试用，提交前关掉）────────────────────────────
        # GENERATION_RESOURCE_TYPES=ppt        只生成 PPT（其它资源可选，用逗号分隔多选，如 ppt,quiz）
        # GENERATION_FAST_MODE=true            跳过最烧时间的反思/一致性/防幻觉重步骤
        _rt_override = (getattr(settings, 'GENERATION_RESOURCE_TYPES', '') or '').strip()
        if _rt_override:
            wanted = [t.strip() for t in _rt_override.replace('，', ',').split(',') if t.strip()]
            if wanted:
                self.resource_types = wanted
        self.fast_mode = bool(getattr(settings, 'GENERATION_FAST_MODE', False))
        if self.fast_mode:
            self.enable_reflection = False  # 快速模式：不做反思重生成
        self.client = XinghuoClient()
        self.results = {}
        # 统计内容生成的 LLM 调用成败：若全部失败(接口彻底不可用)，就把整个生成标记为失败、
        # 给用户明确反馈，而不是写占位内容。
        self._llm_ok = 0
        self._llm_fail = 0
        # Agent协作过程时间线：记录CriticAgent/ReflectionController/StudentSimulatorAgent
        # 各步骤的关键信息，供课程页面可视化"多智能体协作过程"
        self.collaboration_log = []

        # 初始化组件（基于COGENT框架和Instructional Agents）
        self._init_cogent_components()
    
    def _init_cogent_components(self):
        """初始化COGENT框架组件"""
        from .curriculum_standards import CurriculumStandards, StandardsAligner
        from .readability_controller import ReadabilityController
        from .agents import ReflectionController
        from .planner_agent import PlannerAgent
        from .content_quality_evaluator import ContentQualityEvaluator
        from .generation_history import GenerationTracker
        
        self.standards_db = CurriculumStandards()
        self.standards_aligner = StandardsAligner()
        self.readability_controller = ReadabilityController(self.grade_level)
        self.reflection_controller = ReflectionController(
            user=self.user,
            max_iterations=3,
            quality_threshold=self.quality_threshold
        )
        self.planner = PlannerAgent(self.user, self.grade_level)
        self.evaluator = ContentQualityEvaluator(self.grade_level)
        
        # 创建生成会话
        self.session = GenerationTracker.create_session(
            self.user, self.topic, self.grade_level
        )
    
    def generate(self) -> Dict[str, Any]:
        """执行多阶段资源生成（基于Instructional Agents流程）"""

        logger.info(f'========== 开始课程生成 ==========')
        logger.info(f'主题: {self.topic}, 用户: {self.user.id}, outline_id: {self.outline_id}')
        logger.info(f'task: {self.task}, task_id: {self.task.id if self.task else None}')
        logger.info(f'resource_types: {self.resource_types}')

        try:
            logger.info('启动生成会话')
            self.session.start_generation()

            # 阶段1：规划（Plan）
            logger.info('>>> 阶段1：规划 - 获取课程标准')
            self._update_progress('planning', 5, '获取课程标准')
            logger.info('Progress updated to 5%')
            try:
                standards = self._align_with_standards()
                logger.info(f'课程标准获取完成，学科: {standards.get("subject")}')
            except Exception as e:
                logger.warning(f'课程标准获取失败，使用默认: {e}')
                standards = {'subject': '通用', 'concepts': [], 'learning_objectives': [], 'prerequisites': []}

            # 阶段2：生成大纲（Outline）
            logger.info('>>> 阶段2：生成大纲')
            self._update_progress('outlining', 10, '生成课程大纲')
            logger.info('Progress updated to 10%')
            try:
                outline_data = self._generate_outline(standards)
                logger.info(f'大纲生成完成')
            except Exception as e:
                logger.warning(f'大纲生成失败，使用默认结构: {e}')
                outline_data = {'blueprint': self._get_default_outline()}
            # 记住真实生成的大纲，最后把它的章节写回持久化蓝图（否则前端一直显示初始的通用骨架）
            self._generated_outline_data = outline_data
            # 尽早写回真实章节：让前端在 ~10% 就看到 PPO 真大纲，而不是等到 90% 才替换掉通用模板
            self._write_blueprint_chapters_early()

            # 阶段3：生成初稿（Draft）
            logger.info('>>> 阶段3：生成初稿')
            self._update_progress('drafting', 20, '生成初稿')
            logger.info('Progress updated to 20%')
            try:
                self._generate_drafts(outline_data, standards)
                logger.info(f'初稿生成完成，资源类型: {list(self.results.keys())}')
            except Exception as e:
                logger.warning(f'初稿生成失败: {e}')
                # 不再编造占位资源；下面统一按失败反馈用户

            # 若内容生成的 LLM 调用全线失败（接口彻底不可用），直接标记失败并反馈用户，
            # 不产出以假乱真的占位课件。
            if self._llm_unavailable():
                msg = 'AI 接口暂时不可用，课程内容生成失败，请稍后重试。'
                logger.error('内容生成 LLM 全部失败（%d 次），标记课程为失败', self._llm_fail)
                self._mark_outline_failed(msg)
                try:
                    self.session.fail(msg)
                except Exception:
                    pass
                self._update_progress('failed', 0, msg)
                self.results['_generation_error'] = msg
                return self.results

            # 阶段4：审核（Review）
            logger.info('>>> 阶段4：审核内容')
            self._update_progress('review', 50, '审核内容')
            logger.info('Progress updated to 50%')
            try:
                evaluation = self._evaluate_all_content()
                logger.info(f'审核完成，评估项数: {len(evaluation)}')

                self.results['_evaluation'] = evaluation

                avg_score = sum(e.get('overall_score', 0) for e in evaluation.values()) / len(evaluation) if evaluation else 0
                logger.info(f'平均评估分数: {avg_score}, 阈值: {self.quality_threshold}')
                if avg_score < self.quality_threshold and not self.fast_mode:
                    self.enable_reflection = True
                    logger.info('评估分数低于阈值，启用反思改进')
            except Exception as e:
                logger.warning(f'审核失败，跳过反思阶段: {e}')
                self.enable_reflection = False
                self.results['_evaluation'] = {}

            # 阶段5：反思改进（Reflection & Revision）
            if self.enable_reflection:
                logger.info('>>> 阶段5：反思改进')
                self._update_progress('revision', 70, '反思改进')
                logger.info('Progress updated to 70%')
                try:
                    self._review_and_improve()
                    logger.info('反思改进完成')
                except Exception as e:
                    logger.warning(f'反思改进失败: {e}')

            # 阶段5.5：学生模拟个性化适配（StudentSimulatorAgent）
            logger.info('>>> 阶段5.5：学生模拟个性化适配')
            self._update_progress('personalizing', 80, '模拟学生阅读体验')
            logger.info('Progress updated to 80%')
            try:
                self._personalize_for_student()
                logger.info('学生模拟个性化适配完成')
            except Exception as e:
                logger.warning(f'学生模拟失败: {e}')

            # 阶段6：整理输出（Finalize）
            logger.info('>>> 阶段6：整理资源')
            self._update_progress('finalizing', 90, '整理资源')
            logger.info('Progress updated to 90%')
            try:
                self._backfill_slide_code()
                self._save_resources()
                self._update_outline_data()
                logger.info('资源整理完成')
            except Exception as e:
                logger.warning(f'资源整理失败: {e}')

            # 阶段7：完成
            logger.info('>>> 阶段7：生成完成')
            self._update_progress('completed', 100, '生成完成')
            logger.info('Progress updated to 100%')
            self._complete_session()
            logger.info('========== 课程生成完成 ==========')

            return self.results

        except Exception as e:
            logger.exception(f'课程生成失败: {e}')
            self.session.fail(str(e))
            self._update_progress('failed', 0, str(e))
            return self.results
    
    def _align_with_standards(self) -> Dict:
        """获取课程标准对齐信息"""
        standards = self.standards_db.query_standards(self.topic, self.grade_level)
        logger.info(f'课程标准对齐完成：主题={self.topic}, 年级={self.grade_level}')
        return standards
    
    def _generate_outline(self, standards: Dict) -> Dict[str, Any]:
        """生成课程大纲（使用PlannerAgent）。

        若创建课程时 `_build_course_blueprint` 已经用 AI 生成过真实大纲
        (blueprint.outline_source == 'ai')，直接复用，避免二次生成让展示的大纲变来变去。
        """
        # 复用已生成的 AI 大纲
        if self.outline_id:
            try:
                from curriculum_app.models import CourseOutline
                existing = CourseOutline.objects.get(pk=self.outline_id)
                od = existing.outline_data
                if isinstance(od, str):
                    od = json.loads(od)
                bp = (od or {}).get('blueprint') if isinstance(od, dict) else None
                if isinstance(bp, dict) and bp.get('outline_source') == 'ai' and bp.get('chapters'):
                    logger.info('复用创建时已生成的 AI 大纲，跳过重复生成')
                    self._outline_reused = True
                    return {'blueprint': bp}
            except Exception:
                logger.exception('检查已有 AI 大纲失败，改为重新生成')

        self._outline_reused = False
        logger.info(f'开始生成大纲：主题={self.topic}')
        try:
            logger.info(f'调用PlannerAgent.generate_outline')
            outline = self.planner.generate_outline(
                topic=self.topic,
                description='',
                duration=45  # 默认45分钟
            )
            logger.info(f'大纲生成完成，章节数={len(outline.get("chapters", []))}')
            
            # 为每个章节添加"惊奇式学习设计"
            logger.info('开始添加惊奇式学习设计')
            for chapter in outline.get('chapters', []):
                self.planner.add_curiosity_hook(chapter, self.topic)
            logger.info('惊奇式学习设计添加完成')
            
            # 记录到历史
            logger.info('记录大纲到历史')
            self.session.history.add_record('outline', outline)
            logger.info('大纲历史记录完成')

            # 记入协作时间线：规划智能体是多智能体分工的第一环，让每种资源的时间线都从"规划"开始
            _chap_n = len(outline.get('chapters', []))
            for _rt in (self.resource_types or []):
                self._log_collab(_rt, 'PlannerAgent', 'planning',
                                 note=f'规划课程大纲：{_chap_n} 章', chapter_count=_chap_n)

            return {'blueprint': outline}
        except Exception as e:
            logger.exception(f'大纲生成失败，使用默认结构: {e}')
            # 统一包在 blueprint 下，否则 _get_blueprint_chapters 取不到章节、初稿会没有章节可用
            return {'blueprint': self._get_default_outline()}
    
    def _get_default_outline(self) -> Dict:
        """获取默认大纲结构"""
        return {
            'title': f'{self.topic}课程大纲',
            'objectives': [
                f'理解{self.topic}的核心概念',
                f'掌握{self.topic}的基本原理',
                f'能够应用{self.topic}解决实际问题',
            ],
            'chapters': [
                {
                    'number': 1,
                    'title': '概念导入',
                    'teaching_goal': '理解基本概念',
                    'curiosity_hook': f'为什么{self.topic}如此重要？',
                },
                {
                    'number': 2,
                    'title': '核心原理',
                    'teaching_goal': '掌握核心原理',
                    'curiosity_hook': '这些原理如何工作？',
                },
                {
                    'number': 3,
                    'title': '实践应用',
                    'teaching_goal': '能够应用所学',
                    'curiosity_hook': '如何用这些知识解决问题？',
                },
            ],
        }

    def _evaluate_all_content(self) -> Dict:
        """评估所有生成的内容"""
        evaluations = {}
        
        for rtype in self.resource_types:
            if rtype not in self.results:
                continue
            
            # 根据资源类型获取内容
            if rtype == 'quiz':
                # quiz类型使用questions字段
                content = json.dumps(self.results[rtype].get('questions', []), ensure_ascii=False)
            elif rtype == 'code':
                # code类型使用code字段
                content = self.results[rtype].get('code', '')
            else:
                # 其他类型使用content或preview字段
                content = self.results[rtype].get('content', '') or self.results[rtype].get('preview', '')
            
            if content:
                eval_result = self.evaluator.evaluate(self.topic, content, rtype)
                evaluations[rtype] = eval_result
                
                # 记录审核结果
                self.session.history.add_review(eval_result)
        
        return evaluations
    
    def _load_outline_data(self) -> Dict[str, Any]:
        """加载课程大纲数据"""
        from curriculum_app.models import CourseOutline
        
        if not self.outline_id:
            return {}
        
        try:
            outline = CourseOutline.objects.get(pk=self.outline_id)
            data = outline.outline_data
            if isinstance(data, str):
                try:
                    return json.loads(data)
                except Exception:
                    return {}
            return data or {}
        except CourseOutline.DoesNotExist:
            return {}
    
    def _generate_one_resource(self, rtype: str, outline_data: Dict[str, Any], standards: Dict, readability_prompt: str):
        """生成单一类型资源的初稿；自身兜底异常，返回 dict（失败时带 status='failed'）。"""
        try:
            if rtype == 'doc':
                return self._generate_doc_with_standards(outline_data, standards, readability_prompt)
            elif rtype == 'ppt':
                return self._generate_ppt_with_standards(outline_data, standards, readability_prompt)
            elif rtype == 'quiz':
                return self._generate_quiz_with_standards(outline_data, standards)
            elif rtype == 'code':
                return self._generate_code(outline_data)
            elif rtype == 'mindmap':
                return self._generate_mindmap(outline_data)
            elif rtype == 'reading':
                return self._generate_reading(outline_data)
            return None
        except Exception as e:
            logger.exception(f'生成 {rtype} 初稿失败: {e}')
            # 与占位失败结构统一：带 status:'failed'，前端才能按 status==='failed' 命中失败态
            return {
                'title': f'{self.topic} - {rtype}', 'status': 'failed',
                'error': f'该资源生成失败：{e}', 'metadata': {},
            }

    def _generate_drafts(self, outline_data: Dict[str, Any], standards: Dict):
        """生成各类型资源的初稿。doc/ppt/quiz/code/mindmap/reading 相互独立，可并发生成
        （PPT 内部另有分页并发）；并发数由 GENERATION_RESOURCE_CONCURRENCY 控制，默认 1=串行。"""
        readability_prompt = self.readability_controller.build_readability_prompt()
        valid = ('doc', 'ppt', 'quiz', 'code', 'mindmap', 'reading')
        types = [t for t in self.resource_types if t in valid]
        workers = int(getattr(settings, 'GENERATION_RESOURCE_CONCURRENCY', 1) or 1)

        # 串行路径（默认；兼容星火免费档的低并发）
        if workers <= 1 or len(types) <= 1:
            for rtype in types:
                res = self._generate_one_resource(rtype, outline_data, standards, readability_prompt)
                if res is not None:
                    self.results[rtype] = res
            return

        # 并发路径（GPU 自建 Ollama）：各资源并行生成，主线程收集结果写回 self.results
        def _run(rtype):
            from django.db import connection
            try:
                return rtype, self._generate_one_resource(rtype, outline_data, standards, readability_prompt)
            finally:
                connection.close()  # 释放本线程的 SQLite 连接，避免写锁悬挂/连接泄漏

        with ThreadPoolExecutor(max_workers=min(workers, len(types))) as executor:
            futures = [executor.submit(_run, t) for t in types]
            for future in as_completed(futures):
                try:
                    rtype, res = future.result()
                    if res is not None:
                        self.results[rtype] = res
                except Exception as e:
                    logger.exception('并发生成资源线程异常: %s', e)
    
    def _generate_doc_with_standards(self, outline_data: Dict, standards: Dict, readability_prompt: str) -> Dict:
        """生成讲义文档（集成课程标准和可读性控制）"""
        blueprint_text = json.dumps(outline_data.get('blueprint') or {}, ensure_ascii=False)
        learning_objectives = standards.get('learning_objectives', [])
        
        prompt = f"""你是一名资深大学讲师，请为主题"{self.topic}"写一份**有深度、能自学**的教学讲义（Markdown）。

【课程标准】
- 学科：{standards.get('subject', '')}
- 年级水平：{standards.get('grade_level', '')}
- 学习目标：{', '.join(learning_objectives)}

【课程蓝图】
{blueprint_text}

{readability_prompt}

## 讲义结构（用 Markdown，按此组织）
1. 开头用一段"导入"点出这门课要解决什么问题、为什么值得学（不要写"本讲义介绍…"这种套话）。
2. 按课程蓝图的章节逐章展开，**每一章都要包含**：
   - **核心概念**：精确定义（有公式就写出来），并解释它和已知事物的关系；
   - **直觉类比**：一个具体类比，帮学生建立直觉；
   - **原理/推导**：分步骤讲清楚它是怎么工作的，有推导就一步步推、不跳步；
   - **具体例子**：一个**代入真实数字**算一遍的例子（不能只说"举个例子"）；
   - **常见误区**：学生最容易犯的 1-2 个错误理解，并给出正确理解；
   - **练习**：1-2 道能检验真正理解的小题（附参考答案要点）。
3. 结尾一段"本讲小结"，提炼最关键的几点，并给一个引导深入的思考题。

## 硬性要求
- 讲解要具体、有信息量，**禁止空话套话**；每个概念都要真正讲透，像老师在课堂上讲。
- 篇幅充实（建议 1500 字以上），但不注水。
- 直接输出 Markdown 正文，不要任何额外说明或代码块包裹。
"""

        content = self.client.generate_text(prompt, max_tokens=4096)
        content, _ = self._safe_text(content)
        if self._note_llm(content):
            # 生成失败：明确标记，不写占位讲义
            return {
                'title': f'{self.topic} - 教学讲义',
                'content': '',
                'preview': '',
                'status': 'failed',
                'error': '讲义生成失败：AI 接口暂时不可用，请稍后重试。',
                'metadata': {'standards_aligned': True, 'readability_level': self.grade_level},
            }

        def _censor(txt):
            try:
                from .services.safety import censor_text
                return censor_text(txt), True
            except Exception as exc:
                logger.warning('讲义内容安全过滤失败：%s', exc)
                return txt, False

        # 内容安全：违禁词过滤（真实生效的一步）
        content, safety_checked = _censor(content)

        # 防幻觉·真拦截：LLM 事实审校 → 若发现高危事实错误，带着错误清单重生成一次纠正 → 复审 → 只在变好时采纳
        # 快速模式下跳过（这一步会多调 1-3 次 LLM，本地测试时最烧时间）
        review = {} if self.fast_mode else self._llm_fact_review(content)
        corrected = False
        if review.get('reviewed') and review.get('has_errors') and review.get('severity') == 'high':
            self._log_collab('doc', 'FactCheckAgent', 'review', note='发现事实性问题，触发纠正重生成')
            fix_prompt = (prompt + '\n\n【上一稿被学科审校发现以下事实性错误，请在这一稿中逐一纠正，'
                          '确保定义/公式/结论准确，其余要求不变】\n'
                          + '\n'.join('- ' + e for e in review.get('errors') or []))
            try:
                content2 = self.client.generate_text(fix_prompt, max_tokens=4096)
                content2, _ = self._safe_text(content2)
                if not self._note_llm(content2) and content2.strip():
                    content2, _ = _censor(content2)
                    review2 = self._llm_fact_review(content2)
                    # 只有复审"不再有高危错误"才采纳新稿，避免越改越糟
                    if not (review2.get('has_errors') and review2.get('severity') == 'high'):
                        content, review, corrected = content2, review2, True
                        self._log_collab('doc', 'FactCheckAgent', 'correct', note='纠正后复审通过')
            except Exception:
                logger.warning('讲义纠正重生成失败', exc_info=True)

        reliable = not (review.get('has_errors') and review.get('severity') == 'high')
        fact_check = {
            'reviewed': bool(review.get('reviewed')),
            'reliable': reliable,
            'corrected': corrected,
            # 可见证据：审校指出的问题（若已纠正则为空/低危）
            'warnings': [str(e) for e in (review.get('errors') or [])][:4],
        }

        return {
            'title': f'{self.topic} - 教学讲义',
            'content': content,
            'preview': content[:800] if content else '',
            'fact_check': fact_check,
            'metadata': {'standards_aligned': True, 'readability_level': self.grade_level,
                         # 如实反映：安全过滤 + 事实审校是否真的跑了、是否可靠、是否纠正过
                         'content_safety_checked': safety_checked,
                         'fact_reviewed': bool(review.get('reviewed')),
                         'fact_corrected': corrected,
                         'fact_check_reliable': reliable},
        }
    
    def _fallback_skeleton(self, outline_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从 _smart_build_slide_deck 派生骨架，每页携带完整的兜底内容
        （`_fallback_slide`），供单页内容生成失败时直接使用。"""
        smart_slides = _smart_build_slide_deck(self.topic, outline_data)
        skeleton = []
        for slide in smart_slides:
            layout = slide.get('layout', 'two_column')
            visual_blocks = slide.get('visual_blocks') or []
            skeleton.append({
                'layout': layout,
                'theme': slide.get('theme', 'academic_light'),
                'title': slide.get('title', self.topic),
                'teaching_goal': slide.get('teaching_goal', ''),
                'chapter_id': slide.get('chapter_id', ''),
                'content_brief': slide.get('teaching_task') or slide.get('teaching_goal') or '',
                'needs_code': any(isinstance(b, dict) and b.get('kind') == 'code' for b in visual_blocks),
                'needs_animation': layout == 'animation_embed',
                'needs_quiz': layout == 'quiz_check',
                '_fallback_slide': slide,
            })
        return skeleton

    def _generate_deck_skeleton(self, outline_data: Dict[str, Any], standards: Dict[str, Any]) -> List[Dict[str, Any]]:
        """第一阶段：生成PPT教学蓝图（页面顺序、版式、核心解释、具体例子、学生问题）。
        解析失败或页数不足3页时，退化为基于 _smart_build_slide_deck 的模板骨架。"""
        payload = None
        try:
            prompt = build_deck_skeleton_prompt(self.topic, outline_data, standards)
            raw = self.client.generate_text(prompt, max_tokens=3072)
            raw, _ = self._safe_text(raw)
            if self._note_llm(raw):
                logger.warning('PPT教学蓝图生成返回占位文本（API 不可用），改用模板骨架兜底')
                raw = None
            payload = _extract_json_object_robust(raw) if raw else None
        except Exception as exc:
            logger.warning('PPT教学蓝图生成请求失败：%s', exc)

        skeleton = []
        slides_payload = payload.get('slides') if isinstance(payload, dict) else None
        if isinstance(slides_payload, list):
            for item in slides_payload:
                if not isinstance(item, dict):
                    continue
                title = str(item.get('title') or '').strip()
                if not title:
                    continue
                # 兼容旧字段 content_brief 和新字段 core_explanation
                core_explanation = str(item.get('core_explanation') or item.get('content_brief') or '').strip()
                skeleton.append({
                    'layout': _normalize_slide_layout(item.get('layout')),
                    'theme': _normalize_slide_theme(item.get('theme')),
                    'title': title,
                    'teaching_goal': str(item.get('teaching_goal') or '').strip(),
                    'chapter_id': str(item.get('chapter_id') or '').strip(),
                    # 保留旧字段供其他地方使用，同时带上新字段
                    'content_brief': core_explanation,
                    'core_explanation': core_explanation,
                    'concrete_example': str(item.get('concrete_example') or '').strip(),
                    'key_question': str(item.get('key_question') or '').strip(),
                    'one_line': str(item.get('one_line') or '').strip(),
                    'narrative_role': str(item.get('narrative_role') or '').strip(),
                    'needs_code': bool(item.get('needs_code')),
                    'needs_animation': bool(item.get('needs_animation')),
                    'needs_quiz': bool(item.get('needs_quiz')),
                })

        if len(skeleton) >= 3:
            return skeleton

        logger.warning('PPT蓝图生成结果不足3页（实际%d页），改用模板骨架兜底', len(skeleton))
        return self._fallback_skeleton(outline_data)

    def _generate_slide_contents(
        self,
        skeleton: List[Dict[str, Any]],
        outline_data: Dict[str, Any],
        standards: Dict[str, Any],
        readability_prompt: str,
    ) -> List[Dict[str, Any]]:
        """第二阶段：并行为每页骨架生成详细内容（要点/视觉块/讲稿/师生互动）。
        单页生成失败时退化为该页的模板兜底内容（若有）。"""

        # 生成全课叙事上下文并原地挂到每页骨架，供逐页 prompt 注入（纯 Python，不调 LLM）
        _build_deck_narrative(skeleton)

        def _build_one(skeleton_slide: Dict[str, Any]) -> Dict[str, Any]:
            merged = {k: v for k, v in skeleton_slide.items() if k not in ('_fallback_slide', 'narrative_context')}
            prompt = build_slide_content_prompt(
                self.topic, skeleton_slide, outline_data, standards, readability_prompt,
                narrative_context=skeleton_slide.get('narrative_context'),
            )

            content = None
            has_content = False
            for attempt in range(2):
                try:
                    raw = self.client.generate_text(prompt, max_tokens=2048)
                    raw, _ = self._safe_text(raw)
                    if self._note_llm(raw):
                        content = None  # API 占位文本，视为失败以触发重试/兜底
                    else:
                        content = _extract_json_object_robust(raw)
                        if _slide_content_is_placeholder(content):
                            # 大模型把 prompt 示例原样抄回来了，视为失败以触发重试/兜底
                            logger.warning('页面"%s"返回的是 prompt 示例占位内容，重试/兜底', skeleton_slide.get('title'))
                            content = None
                except Exception as exc:
                    logger.warning('页面"%s"内容生成失败（第%d次尝试）：%s', skeleton_slide.get('title'), attempt + 1, exc)
                    content = None

                has_content = isinstance(content, dict) and (
                    (isinstance(content.get('bullets'), list) and content.get('bullets'))
                    or (isinstance(content.get('visual_blocks'), list) and content.get('visual_blocks'))
                    or str(content.get('speaker_notes') or '').strip()
                )
                if has_content:
                    break

            if has_content:
                merged['bullets'] = content.get('bullets') or []
                merged['visual_blocks'] = _ensure_required_visual_blocks(self.topic, skeleton_slide, content.get('visual_blocks') or [])
                notes = str(content.get('speaker_notes') or '').strip()
                if not notes:
                    # 弱模型偶尔把内容都塞进 bullets、漏了讲稿 → 用要点/骨架核心解释兜底，保证数字人有稿可读
                    fallback_src = ' '.join(str(b) for b in (merged['bullets'] or [])).strip() \
                        or str(skeleton_slide.get('core_explanation') or '').strip()
                    title = str(skeleton_slide.get('title') or self.topic).strip()
                    notes = (f'这一页我们看"{title}"。' + fallback_src) if fallback_src else f'这一页我们看"{title}"。'
                merged['speaker_notes'] = notes
                merged['teacher_action'] = str(content.get('teacher_action') or '').strip()
                merged['student_interaction'] = str(content.get('student_interaction') or '').strip()
                return merged

            fallback_slide = skeleton_slide.get('_fallback_slide')
            if isinstance(fallback_slide, dict):
                merged['bullets'] = fallback_slide.get('bullets') or []
                merged['visual_blocks'] = fallback_slide.get('visual_blocks') or []
                merged['speaker_notes'] = fallback_slide.get('speaker_notes') or ''
                merged['teacher_action'] = fallback_slide.get('teacher_action') or ''
                merged['student_interaction'] = fallback_slide.get('student_interaction') or ''
            else:
                logger.warning('页面"%s"内容生成失败，使用兜底内容', skeleton_slide.get('title'))
                fallback_content = _fallback_content_for_skeleton_slide(self.topic, skeleton_slide)
                merged['bullets'] = fallback_content['bullets']
                merged['visual_blocks'] = fallback_content['visual_blocks']
                merged['speaker_notes'] = fallback_content['speaker_notes']
                merged['teacher_action'] = fallback_content['teacher_action']
                merged['student_interaction'] = fallback_content['student_interaction']
            return merged

        results: List[Optional[Dict[str, Any]]] = [None] * len(skeleton)
        # 免费档星火并发能力低（~2），并行太多会触发 5xx/超时；并发数可用 settings 调
        _workers = int(getattr(settings, 'XINGHUO_MAX_CONCURRENCY', 2))
        with ThreadPoolExecutor(max_workers=_workers) as executor:
            future_map = {executor.submit(_build_one, slide): index for index, slide in enumerate(skeleton)}
            for future in as_completed(future_map):
                index = future_map[future]
                try:
                    results[index] = future.result()
                except Exception as exc:
                    logger.warning('第 %d 页内容生成线程异常：%s', index + 1, exc)
                    skeleton_slide = skeleton[index]
                    merged = {k: v for k, v in skeleton_slide.items() if k != '_fallback_slide'}
                    fallback_slide = skeleton_slide.get('_fallback_slide')
                    if isinstance(fallback_slide, dict):
                        merged['bullets'] = fallback_slide.get('bullets') or []
                        merged['visual_blocks'] = fallback_slide.get('visual_blocks') or []
                        merged['speaker_notes'] = fallback_slide.get('speaker_notes') or ''
                        merged['teacher_action'] = fallback_slide.get('teacher_action') or ''
                        merged['student_interaction'] = fallback_slide.get('student_interaction') or ''
                    else:
                        merged.update(_fallback_content_for_skeleton_slide(self.topic, skeleton_slide))
                    results[index] = merged

        return [slide for slide in results if slide]

    def _critique_deck(self, slides: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """连贯层 6a：读整册浓缩视图，只点名"跨页重复/叙事断裂"。
        小进小出，任何异常/占位/非 JSON/越界都安全降级为空列表（→ 跳过重写、发草稿）。"""
        try:
            prompt = build_deck_revision_prompt(self.topic, build_deck_digest(slides))
            raw = self.client.generate_text(prompt, max_tokens=1536)
            raw, _ = self._safe_text(raw)
            if _is_llm_failure(raw):
                return []
            parsed = _extract_json_object_robust(raw) or {}
            revisions = parsed.get('revisions') if isinstance(parsed, dict) else None
            if not isinstance(revisions, list):
                return []
            n = len(slides)
            seen_index = set()
            out = []
            for r in revisions:
                if not isinstance(r, dict):
                    continue
                idx = r.get('index')
                if not isinstance(idx, int) or not (0 <= idx < n) or idx in seen_index:
                    continue
                issues = [x for x in (r.get('issues') or []) if x in ('duplicate_of', 'broken_flow', 'factual_error')]
                if not issues:
                    continue
                seen_index.add(idx)
                out.append({'index': idx, 'issues': issues, 'instruction': str(r.get('instruction') or '')[:200]})
            # 上限 ceil(N/2)，避免退化成整册重生成
            return out[: max(1, math.ceil(n / 2))]
        except Exception as exc:
            logger.warning('连贯层体检失败：%s', exc)
            return []

    @staticmethod
    def _rewrite_not_worse(candidate: Dict[str, Any], draft: Dict[str, Any]) -> bool:
        """硬质量门：重写结果必须不劣于草稿，否则保留草稿（防弱模型误判导致净负回归）。"""
        if not isinstance(candidate, dict):
            return False
        new_bullets = candidate.get('bullets') or []
        if not new_bullets or any(_is_placeholder_text(str(b)) for b in new_bullets):
            return False
        new_notes = str(candidate.get('speaker_notes') or '')
        old_notes = str(draft.get('speaker_notes') or '')
        return len(new_notes) >= 0.8 * len(old_notes)

    def _revise_slides(
        self,
        slides: List[Dict[str, Any]],
        skeleton: List[Dict[str, Any]],
        revisions: List[Dict[str, Any]],
        outline_data: Dict[str, Any],
        standards: Dict[str, Any],
        readability_prompt: str,
    ) -> List[Dict[str, Any]]:
        """连贯层 6b：只并行重写被点名页，过硬质量门才按 index 覆盖，否则保留草稿。"""
        if not revisions:
            return slides
        digests = [build_deck_digest([s]) for s in slides]

        def _revise_one(rev: Dict[str, Any]) -> Tuple[int, Optional[Dict[str, Any]]]:
            idx = rev['index']
            draft = slides[idx]
            skeleton_slide = skeleton[idx] if idx < len(skeleton) else draft
            prompt = build_slide_revision_prompt(
                self.topic, skeleton_slide, draft, rev.get('instruction', ''),
                prev_digest=digests[idx - 1] if idx > 0 else '',
                next_digest=digests[idx + 1] if idx + 1 < len(digests) else '',
            )
            for _ in range(2):
                try:
                    raw = self.client.generate_text(prompt, max_tokens=2048)
                    raw, _ = self._safe_text(raw)
                    if _is_llm_failure(raw):
                        continue
                    content = _extract_json_object_robust(raw)
                    if _slide_content_is_placeholder(content):
                        continue  # 重写把示例抄回来了，保留原草稿
                    if isinstance(content, dict) and self._rewrite_not_worse(content, draft):
                        merged = {k: v for k, v in draft.items()}
                        merged['bullets'] = content.get('bullets') or draft.get('bullets') or []
                        merged['visual_blocks'] = _ensure_required_visual_blocks(
                            self.topic, skeleton_slide, content.get('visual_blocks') or draft.get('visual_blocks') or []
                        )
                        merged['speaker_notes'] = str(content.get('speaker_notes') or '').strip() or draft.get('speaker_notes', '')
                        merged['teacher_action'] = str(content.get('teacher_action') or '').strip() or draft.get('teacher_action', '')
                        merged['student_interaction'] = str(content.get('student_interaction') or '').strip() or draft.get('student_interaction', '')
                        return idx, merged
                except Exception as exc:
                    logger.warning('第 %d 页重写失败：%s', idx + 1, exc)
            return idx, None  # 未通过质量门 → 保留草稿

        _workers = int(getattr(settings, 'XINGHUO_MAX_CONCURRENCY', 2))
        with ThreadPoolExecutor(max_workers=_workers) as executor:
            futures = [executor.submit(_revise_one, rev) for rev in revisions]
            for future in as_completed(futures):
                try:
                    idx, revised = future.result()
                    if revised is not None:
                        slides[idx] = revised
                except Exception as exc:
                    logger.warning('重写线程异常：%s', exc)
        return slides

    def _generate_teaching_lecture(self, outline_data: Dict, standards: Dict) -> str:
        """讲义先行法第一步：生成完整的课堂教学讲义（连贯长文）。"""
        logger.info(f'[讲义先行] 开始生成教学讲义 topic={self.topic}')
        prompt = build_teaching_lecture_prompt(self.topic, outline_data, standards)
        raw = self.client.generate_text(prompt, max_tokens=4096)
        raw, _ = self._safe_text(raw)
        if _is_llm_failure(raw):
            logger.warning('[讲义先行] 讲义生成返回占位文本（API 不可用），返回空串以降级')
            return ''
        lecture = raw.strip()
        logger.info(f'[讲义先行] 讲义生成完毕，字数约 {len(lecture)}')
        return lecture

    def _convert_lecture_to_slides(self, lecture_text: str, outline_data: Dict) -> List[Dict]:
        """讲义先行法第二步：把讲义转换成结构化幻灯片 JSON。"""
        logger.info(f'[讲义先行] 开始将讲义转换为幻灯片结构')
        prompt = build_lecture_to_slides_prompt(self.topic, lecture_text, outline_data)
        raw = self.client.generate_text(prompt, max_tokens=4096)
        raw, _ = self._safe_text(raw)

        try:
            parsed = _extract_json_object_robust(raw)
            slides_raw = parsed.get('slides') if isinstance(parsed, dict) else None
            if isinstance(slides_raw, list) and len(slides_raw) >= 3:
                logger.info(f'[讲义先行] 讲义转换成功，幻灯片数: {len(slides_raw)}')
                return slides_raw
        except Exception as exc:
            logger.warning(f'[讲义先行] 讲义转幻灯片 JSON 解析失败: {exc}')

        logger.warning('[讲义先行] 转换结果不合格，返回空列表（将降级到两阶段法）')
        return []

    def _generate_ppt_with_standards(self, outline_data: Dict, standards: Dict, readability_prompt: str) -> Dict:
        """生成PPT（骨架法转正）：
        骨架全局规划 → 派生叙事上下文 → 逐页并行深化 → 跨页硬去重 → 可选连贯层 → 规范化 → 动画。
        任一 LLM 步骤失败都被隔离，逐页深化之后手上永远有一份可交付的完整 deck。"""
        logger.info(f'=== 开始生成PPT（骨架法） === topic={self.topic}')

        # ── Step 1-3：骨架 + 多样性校正（叙事上下文在 _generate_slide_contents 内派生）──
        skeleton = self._generate_deck_skeleton(outline_data, standards)
        skeleton = _ensure_skeleton_diversity(self.topic, skeleton)
        logger.info(f'PPT骨架生成完成，页数: {len(skeleton)}')

        # ── Step 4：逐页并行深化（每页独立小 JSON，失败隔离到单页）──
        slide_payloads = self._generate_slide_contents(skeleton, outline_data, standards, readability_prompt)

        # ── Step 5：跨页硬去重（软 prompt 兜不住的重复，这里确定性删除）──
        _dedupe_bullets_across_slides(slide_payloads)

        # ── Step 6：连贯层 Phase B（可 settings 开关，默认开；任何失败即发草稿）──
        fact_corrected_slides = 0  # PPT 事实审校纠正了几页（可见证据）
        deck_reviewed = False
        if not self.fast_mode and getattr(settings, 'PPT_ENABLE_COHERENCE_PASS', True) and len(slide_payloads) == len(skeleton):
            try:
                revisions = self._critique_deck(slide_payloads)
                deck_reviewed = True
                fact_corrected_slides = sum(1 for r in revisions if 'factual_error' in (r.get('issues') or []))
                if fact_corrected_slides:
                    self._log_collab('ppt', 'FactCheckAgent', 'correct', note=f'事实审校纠正 {fact_corrected_slides} 页')
                if revisions:
                    slide_payloads = self._revise_slides(
                        slide_payloads, skeleton, revisions, outline_data, standards, readability_prompt
                    )
            except Exception as exc:
                logger.warning('连贯层（Phase B）失败，直接发草稿：%s', exc)

        # ── Step 7：规范化 ──
        slides = normalize_slide_deck({'slides': slide_payloads}, self.topic, outline_data)
        # 出题拆分：多道题/题文混排的页面拆成"每题一页"，避免题目选项挤在一起
        slides = _split_quiz_slides(self.topic, slides)
        logger.info(f'PPT内容生成完成，幻灯片数量: {len(slides)}')
        if not slides:
            logger.error('PPT解析结果为空！')

        # ── 动画（独立生成，加占位哨兵守卫）──
        animation_prompt = build_animation_prompt(self.topic, outline_data)
        animation_raw = self.client.generate_text(animation_prompt)
        if _is_llm_failure(animation_raw):
            animation_raw = ''  # 占位文本不当作真实动画描述，交给 normalize 走空
        animations = normalize_animation_assets(animation_raw, self.topic, outline_data, fallback=False)

        slides, unconsumed_animations = _attach_animation_codes(slides, animations)
        # 由 AI 判断某概念是否需要动画：没生成出真实动画代码的动画块直接去掉，
        # 不再硬塞通用"分步演示"模板（那会让很多页莫名其妙冒出雷同动画）。
        _drop_empty_animation_blocks(self.topic, slides)
        # 动画抽出：把嵌在内容页里的（有真实代码的）动画单独成页，全屏演示
        slides = _split_animation_slides(self.topic, slides)
        # 注意：不再把"没被任何页面消费"的动画硬塞成额外的动画页——只有 AI 在骨架里
        # 明确标了 needs_animation 且真生成出动画的页面才会有动画。

        logger.info(f'PPT生成完成，幻灯片数: {len(slides)}, 动画数: {len(animations)}')

        return {
            'title': f'{self.topic} - 教学PPT',
            'slides': slides,
            'animations': animations,
            'preview': slides_to_markdown(slides, self.topic) if slides else '',
            'metadata': {'slide_count': len(slides), 'animation_count': len(animations), 'standards_aligned': True,
                         # 可见证据：整套 PPT 是否过了事实审校、纠正了几页
                         'fact_reviewed': deck_reviewed, 'fact_corrected_slides': fact_corrected_slides},
        }

    def _generate_quiz_with_standards(self, outline_data: Dict, standards: Dict) -> Dict:
        """生成练习题（集成课程标准）"""
        blueprint_text = json.dumps(outline_data.get('blueprint') or {}, ensure_ascii=False)
        objectives = standards.get('learning_objectives', [])
        
        prompt = f"""
请为主题"{self.topic}"生成5道练习题。

【课程标准】
- 年级水平：{standards.get('grade_level', '')}
- 学习目标：{', '.join(objectives)}

【课程蓝图】
{blueprint_text}

【要求】
1. 题目类型包含选择题和简答题
2. 必须只输出合法的 JSON
3. 格式：{{"questions":[{{"id":1,"type":"single_choice|short_answer","question":"...","options":["选项文字1","选项文字2","选项文字3","选项文字4"],"answer":"...","explanation":"..."}}]}}
4. 题目要检验真正的理解，不是背诵；覆盖定义、机制、应用三个层面
5. 【选择题质量】题干必须完整、自洽、无歧义（不要用容易产生歧义的填空句式）；四个选项内容互不重复、其中恰好一个正确；
   answer 字段必须与某个 options 里的选项文字**完全一致**（不要填 A/B/C 这种字母）；解析要说明"为什么这个对、其它为什么错"。
6. 【简答题】answer 给出要点式参考答案，explanation 说明评分关注点。

【输出】
JSON格式的练习题
"""
        
        raw = self.client.generate_text(prompt)
        raw, _ = self._safe_text(raw)
        if self._note_llm(raw):
            # 生成失败：明确标记，不编造题目
            return {
                'title': f'{self.topic} - 练习题',
                'questions': [],
                'preview': '',
                'status': 'failed',
                'error': '练习题生成失败：AI 接口暂时不可用，请稍后重试。',
                'metadata': {'question_count': 0, 'standards_aligned': True},
            }
        quiz = parse_quiz_json(raw, self.topic)

        return {
            'title': f'{self.topic} - 练习题',
            'questions': quiz.get('questions', []),
            'preview': quiz_md_preview(quiz),
            'metadata': {'question_count': len(quiz.get('questions', [])), 'standards_aligned': True},
        }
    
    def _generate_code(self, outline_data: Dict) -> Dict:
        """生成代码实操案例：优先真·LLM（CodeAgent 角色），失败再回退硬编码模板（仍是真实代码），
        两者都拿不到才明确失败。修掉此前"只认5个硬编码主题、否则空代码"的桩。"""
        prompt = (
            f'你是一名资深工程讲师。请围绕主题"{self.topic}"给出一个**可直接运行的实操代码案例**，要求：\n'
            '1) 选择该主题最合适的编程语言（如与编程无关，则给出能演示该主题核心概念的最贴切的可运行代码，'
            '例如用 Python 做数值/可视化演示）；\n'
            '2) 代码要有清晰注释，讲清每一步在做什么；\n'
            '3) 代码放在 Markdown 代码块里（```语言 ... ```）；\n'
            '4) 代码块之后用 2-4 句话说明这段代码演示了该主题的什么核心点、如何运行；\n'
            '5) 直接输出，不要多余寒暄。'
        )
        text = self.client.generate_text(prompt, max_tokens=2048)
        text, _ = self._safe_text(text)
        if not self._note_llm(text):
            self._log_collab('code', 'CodeAgent', 'generate', note='生成实操代码案例')
            lang = _detect_code_language(text) or 'python'
            code_block = _extract_first_code_block(text)
            return {
                'title': f'{self.topic} - 代码实操案例',
                'code': code_block or text,
                'content': text,
                'language': lang,
                'explanation': '',
                'metadata': {'language': lang, 'source': 'llm'},
            }
        # LLM 失败：回退硬编码模板（覆盖到的主题仍是真实可运行代码，非占位）
        code = _generate_example_code(self.topic)
        if code:
            return {
                'title': f'{self.topic} - 代码实操案例',
                'code': code.get('code', ''),
                'language': code.get('language', 'python'),
                'explanation': code.get('explanation', ''),
                'metadata': {'language': code.get('language', 'python'), 'source': 'template'},
            }
        return {
            'title': f'{self.topic} - 代码实操案例', 'code': '', 'status': 'failed',
            'error': '代码案例生成失败：AI 接口暂时不可用，请稍后重试。', 'metadata': {},
        }

    def _generate_mindmap(self, outline_data: Dict) -> Dict:
        """生成知识点思维导图（Markdown 大纲），真·LLM（MindMapAgent 角色）。"""
        blueprint_text = json.dumps(outline_data.get('blueprint') or {}, ensure_ascii=False)
        prompt = (
            f'为主题"{self.topic}"生成一份结构化的**知识点思维导图**（Markdown 缩进列表），要求：\n'
            '1) 有一个中心主题，至少 4 个一级分支，每个一级分支下 2-4 个二级节点，必要时可到三级；\n'
            '2) 覆盖该主题的核心概念、原理、方法、应用与易错点，突出概念之间的关系；\n'
            '3) 结合下面的课程蓝图，与课程章节对应；\n'
            '4) 【只输出一个 Markdown 缩进列表】：不要用 ``` 代码块包裹，不要 mermaid，不要多余的第二段/第二个列表，不要任何额外说明。\n'
            f'【课程蓝图】{blueprint_text}'
        )
        text = self.client.generate_text(prompt, max_tokens=1600)
        text, _ = self._safe_text(text)
        if self._note_llm(text):
            return {
                'title': f'{self.topic} - 思维导图', 'content': '', 'status': 'failed',
                'error': '思维导图生成失败：AI 接口暂时不可用，请稍后重试。', 'metadata': {'kind': 'mindmap'},
            }
        # 兜底：去掉代码块围栏与其后可能多带出来的第二段（mermaid 等），只保留纯缩进列表
        text = re.sub(r'```[a-zA-Z]*', '', text).strip()
        self._log_collab('mindmap', 'MindMapAgent', 'generate', note='生成知识点思维导图')
        return {
            'title': f'{self.topic} - 思维导图',
            'content': text,
            'preview': text[:400],
            'metadata': {'kind': 'mindmap', 'standards_aligned': True},
        }

    def _generate_reading(self, outline_data: Dict) -> Dict:
        """生成拓展阅读材料清单（Markdown），真·LLM（ReadingAgent 角色）。"""
        prompt = (
            f'为主题"{self.topic}"生成一份**拓展阅读材料推荐清单**，要求：\n'
            '1) 5-8 条推荐，覆盖入门到进阶，按难度分级；\n'
            '2) 每条包含：标题、来源/作者、一句话核心内容、适合什么阶段的学生读；\n'
            '3) 材料类型兼顾经典书籍章节、权威教程/文档、代表性论文或优质博客；\n'
            '4) 【严禁编造链接/URL】：只写你确信真实存在的书名、作者、章节或权威资料名称；'
            '不要输出任何 http 链接、markdown 链接或 `{% ... %}` 之类的占位链接。宁可只给"书名+章节"，也不要编 URL。\n'
            '5) 用纯 Markdown 列表输出（不要用代码块包裹），条理清晰，不要额外说明。'
        )
        text = self.client.generate_text(prompt, max_tokens=1600)
        text, _ = self._safe_text(text)
        if self._note_llm(text):
            return {
                'title': f'{self.topic} - 拓展阅读', 'content': '', 'status': 'failed',
                'error': '拓展阅读生成失败：AI 接口暂时不可用，请稍后重试。', 'metadata': {'kind': 'reading'},
            }
        # 兜底：即便有约束，弱模型仍可能编链接。去掉 {% %} 占位、把 markdown 链接压成纯文本，杜绝假 URL
        text = re.sub(r'\{%[^}]*%\}', '', text)
        text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
        self._log_collab('reading', 'ReadingAgent', 'generate', note='生成拓展阅读材料清单')
        return {
            'title': f'{self.topic} - 拓展阅读',
            'content': text,
            'preview': text[:400],
            'metadata': {'kind': 'reading', 'standards_aligned': True},
        }
    
    def _log_collab(self, resource_type: str, agent: str, stage: str, **extra):
        """记录一条Agent协作过程日志，供课程页面展示协作时间线"""
        self.collaboration_log.append({
            'resource_type': resource_type,
            'agent': agent,
            'stage': stage,
            **extra,
        })

    def _review_and_improve(self):
        """执行反思-改进循环"""
        # 获取所有需要改进的资源类型（排除系统字段）
        resource_types = [rtype for rtype in self.resource_types if rtype not in ['_evaluation', '_session', '_optimization_summary']]
        
        for rtype in resource_types:
            if rtype not in self.results:
                continue
            
            # 根据资源类型获取内容
            if rtype == 'ppt':
                # PPT类型使用slides字段
                content = json.dumps(self.results[rtype].get('slides', []), ensure_ascii=False)
            elif rtype == 'quiz':
                # quiz类型使用questions字段
                content = json.dumps(self.results[rtype].get('questions', []), ensure_ascii=False)
            elif rtype == 'code':
                # code类型使用code字段
                content = self.results[rtype].get('code', '')
            else:
                # 其他类型使用content或preview字段
                content = self.results[rtype].get('content', '') or self.results[rtype].get('preview', '')
            
            if not content:
                continue
            
            # 获取课程标准
            standards = self.standards_db.query_standards(self.topic, self.grade_level)
            
            # 执行反思-改进循环
            improvement_result = self.reflection_controller.iterative_improvement(
                topic=self.topic,
                content=content,
                content_type=rtype,
                standards=standards,
                readability_target=self.grade_level
            )

            # 记录CriticAgent各轮审核与ReflectionController修订到协作时间线
            try:
                iterations_list = improvement_result.get('iterations', [])
                total_iterations = len(iterations_list)
                for it in iterations_list:
                    if it.get('debate_rounds'):
                        self._log_collab(
                            rtype, 'CriticAgent', 'debate',
                            iteration=it['iteration'], score=it['score'],
                            needs_revision=it['needs_revision'], rounds=it['debate_rounds'],
                        )
                    else:
                        self._log_collab(
                            rtype, 'CriticAgent', 'review',
                            iteration=it['iteration'], score=it['score'],
                            needs_revision=it['needs_revision'], feedback=it['feedback'],
                        )
                    if it['needs_revision'] and it['iteration'] < total_iterations:
                        self._log_collab(rtype, 'ReflectionController', 'revision', iteration=it['iteration'])
            except Exception as e:
                logger.warning(f'{rtype} 记录协作时间线失败: {e}')

            # 更新改进后的内容
            if rtype == 'ppt':
                # PPT类型需要解析改进后的JSON
                try:
                    improved_slides = json.loads(improvement_result['final_content'])
                    raw = None
                    if isinstance(improved_slides, dict) and improved_slides.get('slides'):
                        raw = improved_slides['slides']
                    elif isinstance(improved_slides, list):
                        raw = improved_slides
                    if raw:
                        # 必须经 normalize_slide_deck 补齐 layout/theme/visual_blocks；
                        # 且 Critic 输入被截断到 3000 字，改写后极易丢页——页数变少就拒绝，保留原已规范化的 deck
                        normalized = normalize_slide_deck({'slides': raw}, self.topic, {})
                        current = self.results[rtype].get('slides') or []
                        if normalized and len(normalized) >= len(current):
                            self.results[rtype]['slides'] = normalized
                            self.results[rtype]['preview'] = slides_to_markdown(normalized, self.topic)
                except Exception:
                    # 如果解析失败，使用原始内容
                    pass
            elif rtype == 'quiz':
                # quiz类型需要解析改进后的JSON
                try:
                    improved_quiz = json.loads(improvement_result['final_content'])
                    if isinstance(improved_quiz, dict):
                        self.results[rtype]['questions'] = improved_quiz.get('questions', self.results[rtype].get('questions', []))
                    self.results[rtype]['preview'] = quiz_md_preview(self.results[rtype])
                except Exception:
                    pass
            elif rtype == 'code':
                self.results[rtype]['final_code'] = improvement_result['final_content']
            else:
                self.results[rtype]['final_content'] = improvement_result['final_content']
            
            self.results[rtype]['quality_score'] = improvement_result['final_score']
            self.results[rtype]['iterations'] = improvement_result['total_iterations']
            self.results[rtype]['quality_met'] = improvement_result['quality_met']
            
            logger.info(f'{rtype} 反思改进完成：评分={improvement_result["final_score"]}, 迭代次数={improvement_result["total_iterations"]}')

    def _personalize_for_student(self):
        """基于学生画像模拟阅读体验，对核心内容做个性化适配（StudentSimulatorAgent）"""
        if not self.user_profile:
            return
        from .agents import StudentSimulatorAgent
        simulator = StudentSimulatorAgent(self.user, client=self.client)

        for rtype in ('doc', 'ppt'):
            result = self.results.get(rtype)
            if not result or 'error' in result:
                continue
            if rtype == 'ppt':
                content = json.dumps(result.get('slides', []), ensure_ascii=False)[:3000]
            else:
                content = (result.get('final_content') or result.get('content', ''))[:3000]
            if not content:
                continue
            try:
                report = simulator.simulate_reading(self.topic, content, rtype, self.user_profile)
            except Exception as e:
                logger.warning(f'{rtype} 学生模拟阅读失败: {e}')
                continue
            result['student_simulation'] = report
            self._log_collab(rtype, 'StudentSimulatorAgent', 'simulation', **report)

            if rtype == 'doc' and report.get('overall_fit_score', 100) < 80 and (
                report.get('comprehension_issues') or report.get('misconception_triggers') or report.get('suggestions')
            ):
                try:
                    revised = simulator.personalize_revision(self.topic, content, rtype, report)
                    if revised and revised.strip():
                        result['final_content'] = revised
                        result['personalized'] = True
                        self._log_collab(rtype, 'StudentSimulatorAgent', 'personalize')
                except Exception as e:
                    logger.warning(f'doc 个性化改写失败: {e}')

    def _backfill_slide_code(self):
        """PPT 里被标了代码块却没生成出代码的页面，用已生成的『代码实操』资源回填，
        避免出现一个空的深色代码框（弱模型常给 needs_code 页留空 code 字段）。"""
        ppt = self.results.get('ppt')
        code_res = self.results.get('code')
        if not isinstance(ppt, dict) or not isinstance(code_res, dict) or 'error' in code_res:
            return
        real_code = str(code_res.get('final_code') or code_res.get('code') or '').strip()
        if not real_code:
            return
        lang = code_res.get('language') or 'python'
        filled = 0
        for slide in ppt.get('slides') or []:
            if not isinstance(slide, dict):
                continue
            for block in slide.get('visual_blocks') or []:
                if not isinstance(block, dict):
                    continue
                is_code = block.get('kind') in ('code', 'code_block') or block.get('language')
                if is_code and not str(block.get('code') or '').strip():
                    block['code'] = real_code
                    block.setdefault('language', lang)
                    filled += 1
        if filled:
            logger.info(f'回填空代码块 {filled} 处（用代码实操资源）')
            try:
                self._log_collab('ppt', 'CodeAgent', 'backfill', note=f'回填 {filled} 处空代码块')
            except Exception:
                pass

    def _save_resources(self):
        """保存生成的资源到数据库"""
        from .models import LearningResource
        import json

        for rtype, result in self.results.items():
            # 跳过下划线前缀的元数据键（_evaluation/_session/_collaboration_log 等），
            # 它们不是真资源，误存会污染用户资源列表与数据库
            if rtype.startswith('_'):
                continue
            if not isinstance(result, dict) or 'error' in result:
                continue

            # 检查是否已存在相同的资源
            existing_resource = LearningResource.objects.filter(
                author=self.user,
                resource_type=rtype,
                title__icontains=self.topic
            ).first()
            
            # 根据资源类型获取内容（优先使用改进后的内容）
            if rtype == 'code':
                content = result.get('final_code', result.get('code', '')) or ''
            elif rtype == 'ppt':
                # PPT类型使用slides字段
                slides = result.get('slides', [])
                content = json.dumps(slides, ensure_ascii=False)
            elif rtype == 'quiz':
                # quiz类型使用questions字段或改进后的final_content
                final_content = result.get('final_content')
                if final_content:
                    # 改进后的内容可能是JSON字符串
                    try:
                        parsed = json.loads(final_content)
                        if parsed.get('questions'):
                            content = final_content
                        else:
                            content = json.dumps(result.get('questions', []), ensure_ascii=False)
                    except Exception:
                        # 如果不是有效JSON，使用原始questions
                        content = json.dumps(result.get('questions', []), ensure_ascii=False)
                else:
                    content = json.dumps(result.get('questions', []), ensure_ascii=False)
            else:
                content = result.get('final_content', result.get('content', '')) or ''
            
            if existing_resource:
                existing_resource.title = result.get('title', f'{self.topic} - {rtype}')
                existing_resource.content = content
                existing_resource.metadata = result.get('metadata', {})
                existing_resource.save()
                self.results[rtype]['resource_id'] = existing_resource.id
                logger.info(f'Updated existing resource {existing_resource.id} for {self.topic} ({rtype})')
            else:
                resource = LearningResource.objects.create(
                    title=result.get('title', f'{self.topic} - {rtype}'),
                    resource_type=rtype,
                    content=content,
                    metadata=result.get('metadata', {}),
                    author=self.user,
                )
                self.results[rtype]['resource_id'] = resource.id
                logger.info(f'Created new resource {resource.id} for {self.topic} ({rtype})')
    
    def _write_blueprint_chapters_early(self):
        """把阶段2刚生成的真实 AI 大纲章节【尽早】写回持久化蓝图，让前端在 ~10% 就看到真章节/目标，
        而不是等到 90% 的 _update_outline_data。只更新 blueprint，不动 resources/status/progress。"""
        if not self.outline_id:
            return
        try:
            from curriculum_app.models import CourseOutline
            if getattr(self, '_outline_reused', False):
                return  # 复用的已是 AI 版本，无需替换
            display_bp = _planner_blueprint_to_display(getattr(self, '_generated_outline_data', None) or {}, self.topic)
            if not display_bp or not display_bp.get('chapters'):
                return
            outline = CourseOutline.objects.get(pk=self.outline_id)
            cur = outline.outline_data
            cur = json.loads(cur) if isinstance(cur, str) else (cur or {})
            bp = cur.get('blueprint') if isinstance(cur.get('blueprint'), dict) else {}
            bp['chapters'] = display_bp['chapters']
            bp['chapter_count'] = display_bp['chapter_count']
            bp['estimated_hours'] = display_bp['estimated_hours']
            if display_bp.get('objectives'):
                bp['objectives'] = display_bp['objectives']
            cur['blueprint'] = bp
            outline.outline_data = json.dumps(cur, ensure_ascii=False)
            outline.save(update_fields=['outline_data'])
            logger.info('阶段2：已尽早把真实大纲章节写回蓝图，前端可提前看到真章节')
        except Exception:
            logger.exception('尽早写回大纲章节失败（不影响后续）')

    def _update_outline_data(self):
        """更新课程大纲数据"""
        from curriculum_app.models import CourseOutline

        # 记录Agent协作过程时间线（在_save_resources之后才写入self.results，
        # 避免被当作资源类型尝试保存为LearningResource）
        self.results['_collaboration_log'] = self.collaboration_log

        if not self.outline_id:
            return
        
        try:
            outline = CourseOutline.objects.get(pk=self.outline_id)
            current_data = outline.outline_data
            
            if isinstance(current_data, str):
                try:
                    current_data = json.loads(current_data)
                except Exception:
                    current_data = {}
            else:
                current_data = current_data or {}
            
            current_data['generation_phase'] = 'courseware_ready'

            # 用真实生成的大纲章节替换初始的通用骨架（否则前端一直显示“课程导入/核心概念…”那套模板）。
            # 若大纲是复用创建时已生成的 AI 版本，章节已经正确，无需再转换写回。
            try:
                display_bp = None
                if not getattr(self, '_outline_reused', False):
                    display_bp = _planner_blueprint_to_display(getattr(self, '_generated_outline_data', None) or {}, self.topic)
                if display_bp:
                    bp = current_data.get('blueprint') if isinstance(current_data.get('blueprint'), dict) else {}
                    bp['chapters'] = display_bp['chapters']
                    bp['chapter_count'] = display_bp['chapter_count']
                    bp['estimated_hours'] = display_bp['estimated_hours']
                    if display_bp['objectives']:
                        bp['objectives'] = display_bp['objectives']
                    current_data['blueprint'] = bp
                    logger.info(f'蓝图章节已用真实生成的大纲替换：{[c["title"] for c in display_bp["chapters"]]}')
            except Exception:
                logger.exception('用真实大纲替换蓝图章节失败，保留原骨架')

            current_data.setdefault('resources', {}).update(self.results)

            # 内容审核 / 防幻觉总览（给评委可见的"机制证据"）：
            # ①所有内容都过了敏感词/内容安全过滤 ②CriticAgent 多轮审核+辩论轮数
            # ③讲义事实性校验置信度
            try:
                _clog = self.collaboration_log or []
                _review_rounds = sum(1 for c in _clog if c.get('agent') in ('CriticAgent', 'DebateCriticAgent')
                                     or c.get('stage') in ('review', 'debate'))
                _doc = self.results.get('doc') or {}
                current_data['content_review'] = {
                    'safety_filtered': True,
                    'critic_rounds': _review_rounds,
                    'fact_check': _doc.get('fact_check'),
                    'agents': sorted({c.get('agent') for c in _clog if c.get('agent')}),
                }
            except Exception:
                logger.exception('构建内容审核总览失败')

            # 对于PPT，还要添加到顶层方便模板访问
            if 'ppt' in self.results:
                ppt_data = self.results['ppt']
                if ppt_data.get('structured_slides'):
                    current_data['structured_slides'] = ppt_data['structured_slides']
                if ppt_data.get('slides'):
                    current_data['slides'] = ppt_data['slides']
                if ppt_data.get('preview'):
                    current_data['slide_preview'] = ppt_data['preview']
            
            outline.outline_data = json.dumps(current_data, ensure_ascii=False)
            outline.status = 'completed'
            outline.progress = 100
            outline.save()
            logger.info(f'Updated outline_data for outline {self.outline_id} with generated resources')
        except CourseOutline.DoesNotExist:
            logger.warning(f'CourseOutline {self.outline_id} not found when updating resources')
        except Exception as e:
            logger.exception(f'Failed to update outline_data for outline {self.outline_id}: {e}')
    
    def _complete_session(self):
        """完成生成会话"""
        # 获取最终质量评分
        quality_scores = []
        for result in self.results.values():
            if isinstance(result, dict):
                score = result.get('quality_score') or result.get('final_score')
                if score:
                    quality_scores.append(score)
        
        avg_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 0
        
        # 完成会话
        final_content = ''
        if 'doc' in self.results:
            final_content = self.results['doc'].get('final_content', '') or self.results['doc'].get('content', '')
        
        self.session.complete(final_content, avg_quality)
        
        # 添加会话信息到结果
        self.results['_session'] = self.session.to_dict()
        self.results['_optimization_summary'] = self.session.history.get_optimization_summary()
    
    def _mark_outline_failed(self, error_msg: str):
        """把课程大纲标记为生成失败，并把错误信息写进 outline_data 供前端展示。"""
        if not self.outline_id:
            return
        try:
            from curriculum_app.models import CourseOutline
            outline = CourseOutline.objects.get(pk=self.outline_id)
            data = outline.outline_data
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    data = {}
            data = data or {}
            data['generation_error'] = error_msg
            data['generation_phase'] = 'failed'
            outline.outline_data = json.dumps(data, ensure_ascii=False)
            outline.status = 'failed'
            outline.save(update_fields=['outline_data', 'status', 'updated_at'])
            logger.info('CourseOutline %s 已标记为 failed：%s', self.outline_id, error_msg)
        except Exception:
            logger.exception('标记课程失败状态时出错')

    def _fact_check(self, content: str) -> Optional[Dict[str, Any]]:
        """防幻觉/事实性校验，返回 {confidence, reliable, warnings}；失败返回 None（不阻断生成）。"""
        if not content:
            return None
        try:
            from .services.safety import verify_factuality
            fc = verify_factuality(content, self.topic) or {}
            return {
                'confidence': int(fc.get('confidence', 0) or 0),
                'reliable': bool(fc.get('reliable', True)),
                'warnings': [str(w) for w in (fc.get('warnings') or [])][:4],
                'suggestions': [str(s) for s in (fc.get('suggestions') or [])][:4],
            }
        except Exception:
            logger.exception('事实性校验失败')
            return None

    def _llm_fact_review(self, content: str) -> Dict[str, Any]:
        """用 LLM 对内容做【事实性】审校，返回 {reviewed, has_errors, errors, severity}。
        失败/占位/解析不了都视为"未审校"(reviewed=False)，绝不阻断生成。"""
        if not content or not content.strip():
            return {'reviewed': False}
        prompt = (
            f'你是严谨的"{self.topic}"学科审校专家。只挑出会【误导学生的硬性事实错误】：'
            '定义写错、公式或结论错误、概念张冠李戴、明显违背学科共识。\n'
            '【下列一律不算错误，绝对不要报】：措辞是否严谨、表述是否"过于绝对"、是否不够完整、'
            '能否补充更多细节、举例是否更好、文采风格——这些都不是事实错误。\n'
            '判定从严：只有当一句话本身是【错的、会让学生记住错误知识】时才算 error；'
            '只是"可以说得更准确/更完整"绝不算。\n'
            '只输出合法 JSON：{"has_errors": true/false, "severity": "high|low", '
            '"errors": ["具体指出哪句是错的、正确应当是什么", ...]}。'
            '没有确凿的硬错误就返回 {"has_errors": false, "severity": "low", "errors": []}。\n\n'
            f'【待审内容】\n{content[:2500]}'
        )
        try:
            raw = self.client.generate_text(prompt, max_tokens=800)
            raw, _ = self._safe_text(raw)
            if _is_llm_failure(raw):
                return {'reviewed': False}
            data = _extract_json_object_robust(raw)
            if not isinstance(data, dict):
                return {'reviewed': False}
            errors = [str(e).strip() for e in (data.get('errors') or []) if str(e).strip()][:5]
            has_errors = bool(data.get('has_errors')) and len(errors) > 0
            severity = str(data.get('severity') or ('high' if has_errors else 'low')).strip().lower()
            if severity not in ('high', 'low'):
                severity = 'high' if has_errors else 'low'
            return {'reviewed': True, 'has_errors': has_errors, 'errors': errors, 'severity': severity}
        except Exception:
            logger.warning('LLM 事实审校失败', exc_info=True)
            return {'reviewed': False}

    def _note_llm(self, raw: Any) -> bool:
        """记录一次内容 LLM 调用的成败并返回是否失败（占位）。"""
        failed = _is_llm_failure(raw)
        if failed:
            self._llm_fail += 1
        else:
            self._llm_ok += 1
        return failed

    def _llm_unavailable(self) -> bool:
        """内容生成的 LLM 调用是否全线失败（接口彻底不可用）。"""
        return self._llm_fail > 0 and self._llm_ok == 0

    def _update_progress(self, phase: str, progress: int, message: str):
        """更新生成进度（进度只增不减）"""
        # 更新 AgentTask 的进度
        if self.task:
            try:
                # 先从数据库刷新 task 对象，确保获取最新的进度值
                from agent_system.models import AgentTask
                task_obj = AgentTask.objects.get(pk=self.task.id)
                current_progress = task_obj.progress or 0
                
                # 确保进度只增不减
                if progress > current_progress:
                    task_obj.progress = progress
                    task_obj.status = phase
                    task_obj.save(update_fields=['progress', 'status'])
                    # 更新 self.task 引用
                    self.task = task_obj
                    logger.info(f'Updated AgentTask {task_obj.id} progress to {progress}%')
            except AgentTask.DoesNotExist:
                logger.warning(f'AgentTask {self.task.id} not found when updating progress')
            except Exception as e:
                logger.exception(f'Failed to update AgentTask progress: {e}')
        
        # 同时更新 CourseOutline 的进度
        if self.outline_id:
            try:
                from curriculum_app.models import CourseOutline
                outline = CourseOutline.objects.get(pk=self.outline_id)
                current_progress = outline.progress or 0
                # 确保进度只增不减
                if progress > current_progress:
                    outline.progress = progress
                    outline.save(update_fields=['progress'])
                    logger.info(f'Updated CourseOutline {self.outline_id} progress to {progress}%')
            except CourseOutline.DoesNotExist:
                logger.warning(f'CourseOutline {self.outline_id} not found when updating progress')
            except Exception as e:
                logger.exception(f'Failed to update CourseOutline progress: {e}')
        
        logger.info(f'生成进度：{phase} - {progress}% - {message}')
    
    def _safe_text(self, text: str) -> tuple:
        """安全检查文本"""
        from .services.safety import check_text, check_with_xinghuo, censor_text

        try:
            meta = check_with_xinghuo(text)
        except Exception:
            meta = check_text(text)

        if not isinstance(meta, dict):
            meta = {'safe': True, 'labels': []}

        if not meta.get('safe', True):
            text = censor_text(text)

        return text, meta
