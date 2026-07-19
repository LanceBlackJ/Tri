from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

	dependencies = [
		migrations.swappable_dependency(settings.AUTH_USER_MODEL),
		('curriculum_app', '0008_materialweakareaarchive'),
	]

	operations = [
		migrations.CreateModel(
			name='MaterialQuizAdaptivePolicy',
			fields=[
				('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
				('feedback_counts', models.JSONField(blank=True, default=dict)),
				('strategy', models.JSONField(blank=True, default=dict)),
				('updated_at', models.DateTimeField(auto_now=True)),
				('created_at', models.DateTimeField(auto_now_add=True)),
				('course', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='material_quiz_policies', to='curriculum_app.course')),
				('material', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='quiz_policies', to='curriculum_app.coursematerial')),
				('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='material_quiz_policies', to=settings.AUTH_USER_MODEL)),
			],
			options={
				'db_table': 'material_quiz_adaptive_policies',
				'ordering': ['-updated_at'],
				'indexes': [models.Index(fields=['user', 'material'], name='material_qu_user_id_20566d_idx'), models.Index(fields=['course', 'material'], name='material_qu_course__3e0af0_idx'), models.Index(fields=['updated_at'], name='material_qu_updated_4795f8_idx')],
				'unique_together': {('user', 'material')},
			},
		),
	]
