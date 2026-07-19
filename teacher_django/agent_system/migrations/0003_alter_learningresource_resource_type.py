from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('agent_system', '0002_conversation_message'),
    ]

    operations = [
        migrations.AlterField(
            model_name='learningresource',
            name='resource_type',
            field=models.CharField(choices=[('doc', '文档/讲义'), ('ppt', 'PPT'), ('quiz', '练习题'), ('animation', 'H5动画'), ('video', '视频/动画'), ('code', '代码案例')], default='doc', max_length=20),
        ),
    ]