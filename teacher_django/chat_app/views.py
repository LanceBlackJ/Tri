import json
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
 
from core.xunfei_spark import spark_client


@login_required
def chat_interface_view(request):
    """统一使用新的引导式 AI 导师页面。"""
    return render(request, 'tutor/chat.html')


@login_required
def chat_send_message(request):
    """处理聊天消息"""
    if request.method != 'POST':
        return JsonResponse({'error': '仅支持 POST 请求'}, status=405)
    
    try:
        data = json.loads(request.body)
        message = data.get('message', '')
        
        if not message:
            return JsonResponse({'error': '消息不能为空'}, status=400)
        
        # 这里应该结合用户画像来生成更个性化的回答
        # 简化版本：直接调用 AI
        
        if spark_client:
            # 构建对话历史（简化）
            messages = [
                {"role": "user", "content": message}
            ]
            response = spark_client.get_response(messages)
        else:
            response = "AI 功能暂时不可用。请确保已正确配置讯飞星火 API 凭证。"
        
        return JsonResponse({
            'success': True,
            'response': response
        })
        
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)