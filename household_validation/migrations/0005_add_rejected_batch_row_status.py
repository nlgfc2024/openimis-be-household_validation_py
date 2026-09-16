from django.db import migrations, models


ROW_STATUS_CHOICES = [
    ("PENDING", "Pending"),
    ("APPLIED", "Applied"),
    ("SKIPPED", "Skipped"),
    ("REJECTED", "Rejected"),
    ("ERROR", "Error"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("household_validation", "0004_add_upload_attempt_id"),
    ]

    operations = [
        migrations.AlterField(
            model_name="householdvalidationbatchrow",
            name="status",
            field=models.CharField(
                choices=ROW_STATUS_CHOICES,
                default="PENDING",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="historicalhouseholdvalidationbatchrow",
            name="status",
            field=models.CharField(
                choices=ROW_STATUS_CHOICES,
                default="PENDING",
                max_length=32,
            ),
        ),
    ]
