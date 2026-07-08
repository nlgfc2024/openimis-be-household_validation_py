from dataclasses import dataclass


ACTIVE_PROJECT_STATUSES = ("PREPARATION", "IN_PROGRESS")


@dataclass(frozen=True)
class ProjectOption:
    id: str
    name: str
    status: str
    location_id: str


def project_option_from_project(project):
    location_id = getattr(project, "location_id", None)
    if location_id is None:
        location = getattr(project, "location", None)
        location_id = getattr(location, "id", None)
    return ProjectOption(
        id=str(project.id),
        name=project.name,
        status=project.status,
        location_id=str(location_id),
    )
