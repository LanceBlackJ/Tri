from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

	dependencies = [
		migrations.swappable_dependency(settings.AUTH_USER_MODEL),
		('curriculum_app', '0007_materialquizattempt_materialquestionstat'),
	]

	operations = [
		migrations.CreateModel(
			name='MaterialWeakAreaArchive',
			fields=[
				('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
				('knowledge_tag', models.CharField(blank=True, default='', max_length=255)),
				('source_page', models.CharField(blank=True, default='', max_length=50)),
				('source_heading', models.CharField(blank=True, default='', max_length=255)),
				('archived_at', models.DateTimeField(auto_now_add=True)),
				('course', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='weak_area_archives', to='curriculum_app.course')),
				('material', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='weak_area_archives', to='curriculum_app.coursematerial')),
				('question_stat', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='archives', to='curriculum_app.materialquestionstat')),
				('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='material_weak_area_archives', to=settings.AUTH_USER_MODEL)),
			],
			options={
				'db_table': 'material_weak_area_archives',
				'ordering': ['-archived_at'],
				'indexes': [models.Index(fields=['user', 'archived_at'], name='material_we_user_id_8713bb_idx'), models.Index(fields=['course', 'material'], name='material_we_course__8acb1a_idx')],
				'unique_together': {('user', 'question_stat')},
			},
		),
	]
