from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("household_validation", "0003_add_district_validation_role_rights"),
    ]

    operations = [
        migrations.AddField(
            model_name="householdvalidationbatchrow",
            name="upload_attempt_id",
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="historicalhouseholdvalidationbatchrow",
            name="upload_attempt_id",
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
    ]
