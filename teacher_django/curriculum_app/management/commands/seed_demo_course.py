# -*- coding: utf-8 -*-
"""一键灌入可复现的示例课程 + 知识库（供演示 / 换环境重建）。

赛题要求"自行构造至少一门完整高校专业课程的初始知识库/文档集"。本命令创建一门
《线性代数导论》示例课程、一份讲义资料，并按知识点切好带 embedding 的 MaterialChunk，
使课程 AI 答疑的检索(RAG)在全新环境也能开箱可用。

用法：
    python manage.py seed_demo_course              # 幂等：已存在则跳过
    python manage.py seed_demo_course --reset       # 先删同名示例课程再重建
    python manage.py seed_demo_course --user alice   # 指定归属用户(默认 demo_student)
"""
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.utils import timezone

from curriculum_app.models import Course, CourseMaterial, MaterialChunk
from agent_system.services.embeddings import compute_embedding

User = get_user_model()

COURSE_TITLE = '线性代数导论'
COURSE_SUMMARY = '面向大学一年级的线性代数入门课程：从向量、矩阵到特征值与线性方程组。'

# 知识库分块：每块是一个知识点（heading / content / 关键词）
CHUNKS = [
    ('第1章 向量', '向量的概念与运算',
     '向量是既有大小又有方向的量。n 维向量可写成一列 n 个实数 (x1,x2,...,xn)。向量加法按分量相加，'
     '数乘按分量同乘一个标量。向量的模(长度)为各分量平方和的平方根。两个向量的点积等于对应分量乘积之和，'
     '几何上等于 |a||b|cosθ，可用来判断夹角与正交(点积为 0 即垂直)。',
     '向量, 点积, 模, 正交, 数乘'),
    ('第1章 向量', '线性组合与线性相关',
     '若一个向量能表示成若干向量的数乘之和，就称它是这些向量的线性组合。一组向量线性相关，是指其中至少一个'
     '可由其余向量线性组合表示；否则称线性无关。线性无关的向量张成的空间维数等于向量个数。',
     '线性组合, 线性相关, 线性无关, 张成, 维数'),
    ('第2章 矩阵', '矩阵与矩阵乘法',
     '矩阵是按行列排列的数表。矩阵加法按对应元素相加；矩阵乘法 C=AB 中，C 的第 i 行第 j 列元素等于 A 的第 i 行'
     '与 B 的第 j 列的点积，因此要求 A 的列数等于 B 的行数。矩阵乘法不满足交换律(AB≠BA)但满足结合律。',
     '矩阵, 矩阵乘法, 结合律, 交换律, 转置'),
    ('第2章 矩阵', '逆矩阵与单位矩阵',
     '单位矩阵 I 主对角线为 1、其余为 0，满足 AI=IA=A。若存在矩阵 B 使 AB=BA=I，则 B 是 A 的逆矩阵，记作 A⁻¹，'
     '此时称 A 可逆(非奇异)。方阵可逆当且仅当其行列式不为 0。求逆常用高斯-约当消元法。',
     '逆矩阵, 单位矩阵, 可逆, 奇异, 高斯消元'),
    ('第3章 行列式', '行列式的定义与性质',
     '行列式是方阵的一个标量函数。二阶行列式 |a b; c d| = ad-bc。行列式为 0 表示矩阵不可逆、对应向量线性相关。'
     '性质：交换两行行列式变号；某行乘 k 行列式乘 k；一行加上另一行的倍数行列式不变。',
     '行列式, 可逆, 线性相关, 余子式, 展开'),
    ('第4章 线性方程组', '高斯消元与解的结构',
     '线性方程组可写成 Ax=b。高斯消元通过初等行变换把增广矩阵化为行阶梯形，从而求解。解的情况有三种：'
     '唯一解、无穷多解、无解。当系数矩阵秩等于增广矩阵秩且等于未知数个数时有唯一解。',
     '线性方程组, 高斯消元, 行阶梯形, 秩, 增广矩阵'),
    ('第5章 特征值', '特征值与特征向量',
     '对方阵 A，若存在非零向量 v 与标量 λ 使 Av=λv，则 λ 是 A 的特征值，v 是对应的特征向量。特征值由特征方程 '
     'det(A-λI)=0 求出。特征向量表示在 A 作用下方向不变、只被伸缩的方向，λ 是伸缩因子。',
     '特征值, 特征向量, 特征方程, 伸缩, 对角化'),
    ('第5章 特征值', '矩阵对角化及其应用',
     '若方阵 A 有 n 个线性无关的特征向量，则 A 可对角化：A=PDP⁻¹，其中 D 是以特征值为对角元的对角矩阵，P 的列是'
     '对应特征向量。对角化便于计算矩阵幂 Aᵏ=PDᵏP⁻¹，在动力系统、主成分分析(PCA)等中有广泛应用。',
     '对角化, 矩阵幂, PCA, 对角矩阵, 应用'),
]


class Command(BaseCommand):
    help = '灌入可复现的示例课程《线性代数导论》及其知识库分块(带 embedding)。'

    def add_arguments(self, parser):
        parser.add_argument('--user', default='demo_student', help='课程归属用户名(不存在则创建)')
        parser.add_argument('--reset', action='store_true', help='先删除同名示例课程再重建')

    def handle(self, *args, **opts):
        user, created_user = User.objects.get_or_create(
            username=opts['user'], defaults={'email': f"{opts['user']}@demo.local"})
        if created_user:
            user.set_password('demo12345')
            user.save()
            self.stdout.write(f'创建演示用户 {user.username}(初始密码 demo12345)')

        if opts['reset']:
            n, _ = Course.objects.filter(owner=user, title=COURSE_TITLE).delete()
            self.stdout.write(f'已删除旧的示例课程记录 {n} 条')

        course, created = Course.objects.get_or_create(
            owner=user, title=COURSE_TITLE,
            defaults={'summary': COURSE_SUMMARY, 'source_type': 'uploaded',
                      'status': 'published', 'visibility': 'login',
                      'tags': '线性代数,数学,大学', 'published_at': timezone.now()},
        )
        if not created and not opts['reset']:
            self.stdout.write(self.style.WARNING(
                f'示例课程「{COURSE_TITLE}」已存在(id={course.id})，跳过。用 --reset 可重建。'))
            return

        material = CourseMaterial.objects.create(
            course=course, uploaded_by=user, title='线性代数导论 - 讲义',
            material_type='pdf', description='示例知识库讲义(种子数据)',
            processing_status='ready',
            page_count=len({c[0] for c in CHUNKS}),
        )
        # FileField 必填：写入一个占位讲义文本作为可下载文件
        material.file.save('linear_algebra_intro.txt',
                           ContentFile('（示例课程讲义文本，正文以知识库分块形式入库）'.encode('utf-8')), save=True)

        for idx, (heading, sub, content, kw) in enumerate(CHUNKS):
            MaterialChunk.objects.create(
                material=material, chunk_index=idx, source_page=str(idx + 1),
                heading=f'{heading}｜{sub}', content=content, keyword_summary=kw,
                embedding=compute_embedding(f'{heading} {sub} {content} {kw}'),
                metadata={'seed': True},
            )

        self.stdout.write(self.style.SUCCESS(
            f'✅ 已创建示例课程「{COURSE_TITLE}」(id={course.id})，'
            f'资料 1 份、知识库分块 {len(CHUNKS)} 块(均带 embedding)。归属用户：{user.username}'))
