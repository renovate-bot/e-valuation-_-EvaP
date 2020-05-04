# Generated by Django 2.2.9 on 2020-01-20 21:47

from django.db import migrations, models


def move_grading_information_to_evaluation(apps, _schema_editor):
    Evaluation = apps.get_model('evaluation', 'Evaluation')
    Evaluation.objects.filter(course__is_graded=False).update(wait_for_grade_upload_before_publishing=False)


def move_grading_information_to_course(apps, _schema_editor):
    Course = apps.get_model('evaluation', 'Course')
    Course.objects.exclude(evaluations__wait_for_grade_upload_before_publishing=True).update(is_graded=False)


class Migration(migrations.Migration):

    dependencies = [
        ('evaluation', '0110_semester_is_active'),
    ]

    operations = [
        migrations.AddField(
            model_name='evaluation',
            name='wait_for_grade_upload_before_publishing',
            field=models.BooleanField(default=True, verbose_name='wait for grade upload before publishing'),
        ),
        migrations.RunPython(
            move_grading_information_to_evaluation,
            reverse_code=move_grading_information_to_course
        ),
        migrations.RemoveField(
            model_name='course',
            name='is_graded',
        ),
    ]
