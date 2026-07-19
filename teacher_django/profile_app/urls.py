from django.urls import path
from . import views

urlpatterns = [
    path('', views.profile_dashboard, name='profile_dashboard'),  # 新的用户画像仪表盘入口
    path('building/', views.profile_building_view, name='profile_building'),
    path('view/', views.profile_view, name='profile_view'),
    path('detailed/', views.detailed_profile_view, name='detailed_profile'),
    path('building/step/', views.profile_building_step, name='profile_building_step'),
    path('building/stream/', views.profile_building_stream, name='profile_building_stream'),
    path('building/regenerate/', views.profile_building_regenerate, name='profile_building_regenerate'),
]