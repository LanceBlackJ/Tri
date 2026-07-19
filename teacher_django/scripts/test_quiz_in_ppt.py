"""
测试PPT中的Quiz功能
"""
import os
import sys
import django

# 设置Django环境
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'teacher_django.settings')
django.setup()

from agent_system.generation import normalize_slide_deck, build_slide_deck_prompt

def test_quiz_in_ppt():
    """测试PPT中包含quiz题目的情况"""
    
    # 模拟AI返回的包含quiz的PPT数据
    mock_ppt_data = {
        "analysis": {
            "subject": "机器学习",
            "difficulty": "基础",
            "teaching_strategy": "从概念到实践"
        },
        "slides": [
            {
                "layout": "cover",
                "title": "梯度下降入门",
                "teaching_goal": "理解梯度下降的基本概念",
                "bullets": ["梯度下降是优化算法", "用于最小化损失函数"],
                "speaker_notes": "欢迎学习梯度下降！"
            },
            {
                "layout": "quiz_check",
                "title": "来，测测你掌握了吗？",
                "teaching_goal": "检验学习效果",
                "visual_blocks": [
                    {
                        "kind": "question",
                        "label": "问题1：梯度下降沿什么方向更新参数？",
                        "question_text": "梯度下降沿什么方向更新参数？",
                        "question_type": "choice",
                        "choices": [
                            {"label": "A. 梯度方向", "value": "A"},
                            {"label": "B. 负梯度方向", "value": "B"},
                            {"label": "C. 随机方向", "value": "C"},
                            {"label": "D. 零方向", "value": "D"}
                        ],
                        "correct_answer": "B",
                        "explanation": "梯度下降沿着负梯度方向更新参数，因为负梯度方向是损失函数下降最快的方向。"
                    },
                    {
                        "kind": "question",
                        "label": "问题2：学习率太大有什么问题？",
                        "question_text": "学习率太大有什么问题？",
                        "question_type": "choice",
                        "choices": [
                            {"label": "A. 收敛太慢", "value": "A"},
                            {"label": "B. 可能发散", "value": "B"},
                            {"label": "C. 没有问题", "value": "C"}
                        ],
                        "correct_answer": "B",
                        "explanation": "学习率太大可能导致参数更新步长过大，从而跳过最优解甚至发散。"
                    }
                ],
                "speaker_notes": "现在来做几道测试题！"
            }
        ]
    }
    
    # 测试normalize函数
    topic = "梯度下降"
    outline_data = {"blueprint": {}}
    
    normalized = normalize_slide_deck(mock_ppt_data, topic, outline_data)
    
    print("=" * 60)
    print("测试结果：")
    print("=" * 60)
    
    # 验证结果
    assert len(normalized) >= 2, f"应该至少有2页幻灯片，实际有{len(normalized)}页"
    
    # 检查quiz页面
    quiz_slide = None
    for slide in normalized:
        if slide.get('layout') == 'quiz_check':
            quiz_slide = slide
            break
    
    assert quiz_slide is not None, "应该有一个quiz_check页面"
    print("[OK] 找到quiz_check页面")
    
    # 检查visual_blocks
    visual_blocks = quiz_slide.get('visual_blocks', [])
    assert len(visual_blocks) >= 2, f"quiz页面应该至少有2个题目，实际有{len(visual_blocks)}个"
    print(f"[OK] quiz页面有{len(visual_blocks)}个题目")
    
    # 检查第一个题目的数据结构
    first_question = visual_blocks[0]
    assert first_question.get('kind') == 'question', "第一个visual_block应该是question类型"
    assert 'question_text' in first_question, "题目应该有question_text字段"
    assert 'choices' in first_question, "题目应该有choices字段"
    assert 'correct_answer' in first_question, "题目应该有correct_answer字段"
    assert 'explanation' in first_question, "题目应该有explanation字段"
    print("[OK] 题目数据结构正确")
    
    # 打印详细信息
    print("\n题目详情：")
    for i, block in enumerate(visual_blocks, 1):
        if block.get('kind') == 'question':
            print(f"\n题目{i}: {block.get('question_text')}")
            print(f"选项数量: {len(block.get('choices', []))}")
            print(f"正确答案: {block.get('correct_answer')}")
            print(f"解析: {block.get('explanation')[:50]}...")
    
    print("\n" + "=" * 60)
    print("所有测试通过！[OK]")
    print("=" * 60)

def test_build_slide_deck_prompt():
    """测试PPT生成提示词是否包含quiz相关说明"""
    topic = "梯度下降"
    outline_data = {"blueprint": {"chapters": []}}
    
    prompt = build_slide_deck_prompt(topic, outline_data)
    
    print("\n" + "=" * 60)
    print("测试PPT生成提示词：")
    print("=" * 60)
    
    # 检查提示词是否包含quiz相关内容
    assert "quiz_check" in prompt, "提示词应该包含quiz_check页面类型"
    assert "question" in prompt, "提示词应该包含question类型说明"
    assert "correct_answer" in prompt, "提示词应该包含correct_answer字段说明"
    assert "explanation" in prompt, "提示词应该包含explanation字段说明"
    
    print("[OK] 提示词包含quiz_check页面类型")
    print("[OK] 提示词包含question类型说明")
    print("[OK] 提示词包含correct_answer字段说明")
    print("[OK] 提示词包含explanation字段说明")
    
    # 检查是否有quiz示例
    if "自测页" in prompt or "quiz_check" in prompt:
        print("[OK] 提示词包含quiz示例")
    
    print("\n" + "=" * 60)
    print("提示词测试通过！[OK]")
    print("=" * 60)

if __name__ == "__main__":
    print("\n开始测试PPT中的Quiz功能...\n")
    
    try:
        test_quiz_in_ppt()
        test_build_slide_deck_prompt()
        print("\n[SUCCESS] 所有测试通过！Quiz功能已正确集成到PPT中。")
    except AssertionError as e:
        print(f"\n[FAIL] 测试失败: {e}")
        import traceback
        traceback.print_exc()
    except Exception as e:
        print(f"\n[ERROR] 测试出错: {e}")
        import traceback
        traceback.print_exc()
