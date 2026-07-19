from django.urls import path
from . import views

app_name = 'agent_system'

urlpatterns = [
    path('', views.overview, name='agent_overview'),
    path('generator/', views.generator_page, name='generator_page'),
    path('api/profile/build/', views.api_build_profile, name='api_build_profile'),
    path('api/task/create/', views.api_create_task, name='api_create_task'),
    path('api/multi/generate/', views.api_generate_multi_resources, name='api_generate_multi_resources'),
    path('api/task/<int:pk>/status/', views.api_task_status, name='api_task_status'),
    path('api/resource/search/', views.api_search_resources, name='api_search_resources'),
    path('api/resource/<int:pk>/export/', views.api_export_resource, name='api_export_resource'),
    path('api/resource/<int:pk>/embed/', views.api_compute_embedding, name='api_compute_embedding'),
    path('api/resource/nearest/', views.api_nearest_resources, name='api_nearest_resources'),
    path('api/quiz/grade/', views.api_quiz_grade, name='api_quiz_grade'),
    path('api/profile/get/', views.api_get_profile, name='api_get_profile'),
    path('api/stream/generate/', views.api_stream_generate, name='api_stream_generate'),
    path('api/conversation/send/', views.api_conversation_send, name='api_conversation_send'),
    path('api/conversation/stream/', views.api_conversation_stream, name='api_conversation_stream'),
    path('api/conversation/history/', views.api_conversation_history, name='api_conversation_history'),
    path('api/conversation/delete/', views.api_conversation_delete, name='api_conversation_delete'),
]
