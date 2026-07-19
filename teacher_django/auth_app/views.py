from django.shortcuts import render, redirect
from django.contrib.auth import login, logout
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django import forms
from core.models import User


class CustomUserCreationForm(forms.ModelForm):
    """自定义用户注册表单"""
    password1 = forms.CharField(
        label='密码',
        widget=forms.PasswordInput(attrs={'minlength': '6', 'maxlength': '50'}),
        min_length=6,
        max_length=50
    )
    password2 = forms.CharField(
        label='确认密码',
        widget=forms.PasswordInput(attrs={'minlength': '6', 'maxlength': '50'}),
        min_length=6,
        max_length=50
    )
    full_name = forms.CharField(max_length=50, required=False)
    major = forms.CharField(max_length=50, required=False)
    grade = forms.CharField(max_length=50, required=False)
    
    class Meta:
        model = User
        fields = ('username', 'email', 'full_name', 'major', 'grade')
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['email'].required = True

    def clean_password2(self):
        password1 = self.cleaned_data.get('password1')
        password2 = self.cleaned_data.get('password2')
        if password1 and password2 and password1 != password2:
            raise forms.ValidationError('两次输入的密码不一致')
        return password2

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data['password1'])
        if commit:
            user.save()
        return user


def login_view(request):
    """登录视图"""
    context = {'entered_username': ''}
    if request.method == 'POST':
        username = (request.POST.get('username') or '').strip()
        password = request.POST.get('password') or ''
        context['entered_username'] = username
        
        # 支持用户名或邮箱登录
        try:
            if '@' in username:
                user = User.objects.get(email=username)
                username = user.username
            else:
                user = User.objects.get(username=username)
        except User.DoesNotExist:
            user = None
        
        # 验证密码
        if user is not None and user.check_password(password):
            login(request, user)
            messages.success(request, '登录成功！')
            return redirect('home')
        else:
            messages.error(request, '用户名或密码错误')

    return render(request, 'auth/login.html', context)


def register_view(request):
    """注册视图"""
    if request.method == 'POST':
        form = CustomUserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, '注册成功！欢迎加入学习系统。')
            return redirect('home')
        else:
            messages.error(request, '注册失败，请检查输入信息')
    else:
        form = CustomUserCreationForm()
    
    return render(request, 'auth/register.html', {'form': form})


def logout_view(request):
    """登出视图"""
    logout(request)
    messages.info(request, '已成功登出')
    return redirect('login')


@login_required
def profile_view(request):
    """用户资料视图"""
    if request.method == 'POST':
        user = request.user
        user.full_name = request.POST.get('full_name', '')
        user.major = request.POST.get('major', '')
        user.grade = request.POST.get('grade', '')
        
        # 处理头像上传
        if 'avatar' in request.FILES:
            user.avatar = request.FILES['avatar']
        
        user.save()
        messages.success(request, '资料更新成功！')
        return redirect('profile')
    
    return render(request, 'auth/profile.html')