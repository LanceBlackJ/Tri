from django import forms

from .models import Course, CourseMaterial


class TeacherCourseForm(forms.ModelForm):
    class Meta:
        model = Course
        fields = ['title', 'summary', 'description', 'visibility', 'tags']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'w-full px-3 py-2 border border-slate-300 rounded-md', 'placeholder': '例如：高等数学期中复习'}),
            'summary': forms.Textarea(attrs={'class': 'w-full px-3 py-2 border border-slate-300 rounded-md', 'rows': 2, 'placeholder': '一句话概述课程定位'}),
            'description': forms.Textarea(attrs={'class': 'w-full px-3 py-2 border border-slate-300 rounded-md', 'rows': 5, 'placeholder': '介绍课程目标、适用对象和学习方式'}),
            'visibility': forms.Select(attrs={'class': 'w-full px-3 py-2 border border-slate-300 rounded-md'}),
            'tags': forms.TextInput(attrs={'class': 'w-full px-3 py-2 border border-slate-300 rounded-md', 'placeholder': '标签之间用逗号分隔'}),
        }


class CourseMaterialForm(forms.ModelForm):
    """
    上传表单只保留老师真正要填的东西：标题（可留空，自动取文件名）、说明、文件。
    类型（material_type）改为按文件扩展名自动识别；顺序（display_order）改为
    自动排到末尾——都不需要老师手动选。
    """
    class Meta:
        model = CourseMaterial
        fields = ['title', 'description', 'file']
        widgets = {
            'title': forms.TextInput(attrs={
                'class': 'w-full px-3 py-2 border border-slate-300 rounded-md',
                'placeholder': '留空则自动使用文件名',
                'id': 'materialTitleInput',
            }),
            'description': forms.Textarea(attrs={'class': 'w-full px-3 py-2 border border-slate-300 rounded-md', 'rows': 3, 'placeholder': '说明这份资料的用途（可选）'}),
            'file': forms.ClearableFileInput(attrs={'class': 'material-file-input', 'id': 'materialFileInput'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 标题允许留空：视图会在保存前用文件名自动补上
        self.fields['title'].required = False
