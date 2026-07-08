from django.db import migrations

HOUSEHOLD_VALIDATION_RIGHTS = [958001, 958002, 958003, 958004]
IMIS_ADMINISTRATOR_SYSTEM_ROLE = 64
AUDIT_USER_ID = 1


def add_rights(apps, schema_editor):
    Role = apps.get_model("core", "Role")
    RoleRight = apps.get_model("core", "RoleRight")

    admin_roles = Role.objects.filter(
        is_system=IMIS_ADMINISTRATOR_SYSTEM_ROLE,
        validity_to__isnull=True,
    )
    for role in admin_roles:
        for right_id in HOUSEHOLD_VALIDATION_RIGHTS:
            RoleRight.objects.get_or_create(
                role=role,
                right_id=right_id,
                validity_to=None,
                defaults={"audit_user_id": AUDIT_USER_ID},
            )


def remove_rights(apps, schema_editor):
    RoleRight = apps.get_model("core", "RoleRight")
    RoleRight.objects.filter(
        role__is_system=IMIS_ADMINISTRATOR_SYSTEM_ROLE,
        right_id__in=HOUSEHOLD_VALIDATION_RIGHTS,
        validity_to__isnull=True,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0015_missing_roles"),
    ]

    operations = [
        migrations.RunPython(add_rights, remove_rights),
    ]
