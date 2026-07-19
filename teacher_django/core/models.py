from django.db import models
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
import bcrypt


class UserManager(BaseUserManager):
    """自定义用户管理器"""
    def create_user(self, username, email, password=None, **extra_fields):
        if not email:
            raise ValueError('The Email field must be set')
        email = self.normalize_email(email)
        user = self.model(username=username, email=email, **extra_fields)
        if password:
            # 使用 bcrypt 哈希密码（与原 Next.js 项目一致）
            password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
            user.password_hash = password_hash
        else:
            user.password_hash = ''  # 空密码
        user.save(using=self._db)
        return user

    def create_superuser(self, username, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(username, email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """
    自定义用户模型，对应原 Next.js 项目的 users 表
    完整包含 Django 认证系统所需的所有字段
    """
    id = models.AutoField(primary_key=True)  # 显式定义主键
    
    username = models.CharField(max_length=150, unique=True)
    email = models.EmailField(unique=True)
    password_hash = models.TextField(blank=True, null=True)  # 允许为空，兼容现有数据
    
    # 用户信息字段
    full_name = models.CharField(max_length=50, blank=True, null=True)
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    major = models.CharField(max_length=50, blank=True, null=True)
    grade = models.CharField(max_length=50, blank=True, null=True)
    
    # Django 认证必需字段
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    last_login = models.DateTimeField(blank=True, null=True)  # 添加缺失的字段
    date_joined = models.DateTimeField(auto_now_add=True)     # 添加注册时间字段
    
    # 时间戳字段
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    objects = UserManager()
    
    USERNAME_FIELD = 'username'
    REQUIRED_FIELDS = ['email']

    class Meta:
        db_table = 'users'
        indexes = [
            models.Index(fields=['username']),
            models.Index(fields=['email']),
        ]

    def __str__(self):
        return self.username
    
    # Django 认证系统需要的属性和方法
    @property
    def password(self):
        """Django 需要 password 属性，返回 password_hash"""
        return self.password_hash or ''
    
    @password.setter
    def password(self, raw_password):
        """设置密码时使用 bcrypt 哈希"""
        if raw_password:
            self.password_hash = bcrypt.hashpw(raw_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        else:
            self.password_hash = ''
    
    def set_password(self, raw_password):
        """设置密码方法"""
        self.password = raw_password
    
    def check_password(self, raw_password):
        """验证密码方法"""
        if not self.password_hash:
            return False
        try:
            return bcrypt.checkpw(raw_password.encode('utf-8'), self.password_hash.encode('utf-8'))
        except Exception:
            return False
    
    def save(self, *args, **kwargs):
        # 移除 force_update 参数，让 Django 自动处理创建/更新
        if 'force_update' in kwargs:
            kwargs.pop('force_update')
        super().save(*args, **kwargs)