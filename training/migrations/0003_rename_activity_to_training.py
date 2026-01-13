from django.db import migrations

class Migration(migrations.Migration):

    dependencies = [
        ("training", "0002_person_category_person_current_deployment_person_dob_and_more"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="Activity",
            new_name="Training",
        ),
        migrations.RenameField(
            model_name="participation",
            old_name="activity",
            new_name="training",
        ),
    ]
