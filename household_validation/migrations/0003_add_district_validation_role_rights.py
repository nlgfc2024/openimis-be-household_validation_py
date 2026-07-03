from django.db import migrations


AUDIT_USER_ID = 1
CUSTOM_ROLE_IS_SYSTEM = 0

DISTRICT_VALIDATION_ROLE_UUIDS = {
    "District Administrator": "87de042b-4a4b-43a3-8b89-b2fb4fe8bf33",
    "District Program Manager": "062810c8-6ee4-4e8c-aa49-caf07daa8185",
    "District User": "4cddc8f0-4411-4f1f-9d22-c864b69d3dc4",
}

HOUSEHOLD_VALIDATION_RIGHTS = [958001, 958002, 958003, 958004]
GROUP_ACCESS_RIGHTS = [180001, 180003]

DISTRICT_VALIDATION_ROLE_RIGHTS = {
    "District Administrator": HOUSEHOLD_VALIDATION_RIGHTS + GROUP_ACCESS_RIGHTS,
    "District Program Manager": HOUSEHOLD_VALIDATION_RIGHTS + GROUP_ACCESS_RIGHTS,
    "District User": HOUSEHOLD_VALIDATION_RIGHTS + GROUP_ACCESS_RIGHTS,
}


def create_or_assign_district_validation_roles(apps, schema_editor):
    Role = apps.get_model("core", "Role")
    RoleRight = apps.get_model("core", "RoleRight")

    for role_name, right_ids in DISTRICT_VALIDATION_ROLE_RIGHTS.items():
        role = Role.objects.filter(
            name=role_name,
            validity_to__isnull=True,
        ).first()
        if role is None:
            role = Role.objects.create(
                uuid=DISTRICT_VALIDATION_ROLE_UUIDS[role_name],
                name=role_name,
                alt_language=None,
                is_system=CUSTOM_ROLE_IS_SYSTEM,
                is_blocked=False,
                audit_user_id=AUDIT_USER_ID,
            )
        for right_id in right_ids:
            RoleRight.objects.get_or_create(
                role=role,
                right_id=right_id,
                validity_to=None,
                defaults={"audit_user_id": AUDIT_USER_ID},
            )


def remove_district_validation_role_rights(apps, schema_editor):
    # Do not remove rights from deployment roles on rollback; target
    # environments may have assigned the same role-rights outside this module.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("household_validation", "0002_add_validation_batch_tracking"),
    ]

    operations = [
        migrations.RunPython(
            create_or_assign_district_validation_roles,
            remove_district_validation_role_rights,
        ),
    ]
