from django.urls import path
from . import views

urlpatterns = [
    path('interface/', views.chat_interface_view, name='chat_interface'),
    path('send/', views.chat_send_message, name='chat_send'),
]