from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('api/compare/', views.compare_ajax, name='compare_ajax'),
    path('api/chat/',    views.chat_ajax,    name='chat_ajax'),
    path('api/suggest/', views.city_suggest, name='city_suggest'),
]