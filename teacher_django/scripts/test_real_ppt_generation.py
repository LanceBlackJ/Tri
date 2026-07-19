#!/usr/bin/env python
"""用真实讯飞接口跑一次完整的 _generate_ppt_with_standards 流程，检查骨架与正文中的
needs_code/needs_animation/needs_quiz 是否被合理设置（含 _ensure_skeleton_diversity 安全网），
最终 slides 中是否真的出现了 code/animation/question 视觉块，以及未被消费的动画是否被
追加为独立的 animation_embed 页（_build_animation_slide）。"""
import os
import sys
import json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'teacher_django.settings')
import django
django.setup()

from django.contrib.auth import get_user_model
from agent_system.generation import GenerationManager, _ensure_skeleton_diversity

User = get_user_model()
user = User.objects.filter(username='ppt_real_test').first()
if not user:
    user = User.objects.create_user(username='ppt_real_test', email='ppt_real_test@example.com', password='TestPass123')

TOPIC = sys.argv[1] if len(sys.argv) > 1 else '梯度下降'

manager = GenerationManager(user, TOPIC)
standards = manager.standards_db.query_standards(manager.topic, manager.grade_level)
outline_data = {'blueprint': {'chapters': []}}

print(f'=== 主题: {TOPIC} ===')

print('\n--- 阶段1：生成骨架 ---')
skeleton = manager._generate_deck_skeleton(outline_data, standards)
skeleton = _ensure_skeleton_diversity(TOPIC, skeleton)
print(f'骨架页数: {len(skeleton)}')
for i, s in enumerate(skeleton, 1):
    print(f"{i}. layout={s.get('layout'):14} title={s.get('title')!r:30} "
          f"needs_code={s.get('needs_code')!s:5} needs_animation={s.get('needs_animation')!s:5} needs_quiz={s.get('needs_quiz')!s:5}")
    if s.get('content_brief'):
        print(f"     brief: {s.get('content_brief')}")

# 固定骨架，确保 _generate_ppt_with_standards 内部使用的是同一份骨架（而不是再发起一次
# 非确定性的骨架生成请求），这样下面打印的 needs_code/needs_animation/needs_quiz 才能
# 与最终页面一一对应。
import copy
cached_skeleton = copy.deepcopy(skeleton)
manager._generate_deck_skeleton = lambda *a, **k: copy.deepcopy(cached_skeleton)

print('\n--- 阶段2 + 动画 + 规范化（完整 _generate_ppt_with_standards）---')
result = manager._generate_ppt_with_standards(outline_data, standards, '')
slides = result['slides']
animations = result['animations']
print(f'正文页数: {len(slides)}, 动画决策数: {len(animations)}')

for i, s in enumerate(slides, 1):
    flags = skeleton[i - 1] if i - 1 < len(skeleton) else {}
    kinds = [b.get('kind') for b in (s.get('visual_blocks') or []) if isinstance(b, dict)]
    print(f"{i}. layout={s.get('layout'):14} title={s.get('title')!r:30} "
          f"needs(code={flags.get('needs_code')!s:5} anim={flags.get('needs_animation')!s:5} quiz={flags.get('needs_quiz')!s:5}) "
          f"visual_blocks kinds={kinds}")
    for b in (s.get('visual_blocks') or []):
        if isinstance(b, dict) and b.get('kind') == 'question':
            print(f"     [QUIZ] {b.get('question_text')}")
            print(f"            choices={b.get('choices')}")
            print(f"            correct_answer={b.get('correct_answer')} explanation={b.get('explanation')}")
        if isinstance(b, dict) and b.get('kind') == 'code':
            code_preview = (b.get('code') or '')[:120].replace('\n', ' | ')
            print(f"     [CODE] lang={b.get('language')} code={code_preview}")
        if isinstance(b, dict) and b.get('kind') == 'animation':
            has_code = bool(b.get('animation_code'))
            print(f"     [ANIM] concept={b.get('concept_name')} usage_note={b.get('usage_note')} has_code={has_code}")

print('\n--- 完整 JSON（截断显示前4000字符）---')
dump = json.dumps(slides, ensure_ascii=False, indent=2)
print(dump[:4000])
