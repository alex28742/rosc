from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from .models import Project, Task


def project_directory(workspace_root: Path, project_id: int) -> Path:
    return workspace_root / "projects" / f"P{project_id:04d}"


def project_manifest_path(workspace_root: Path, project_id: int) -> Path:
    return project_directory(workspace_root, project_id) / "project.yaml"


def write_yaml_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".rosc-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(payload, handle, allow_unicode=True, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def provision_project(workspace_root: Path, project: Project) -> None:
    directory = project_directory(workspace_root, project.id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "architect").mkdir(exist_ok=True)
    (directory / "tasks").mkdir(exist_ok=True)
    write_yaml_atomic(project_manifest_path(workspace_root, project.id), {
        "format": "rosc.project.v1",
        "project_id": f"P{project.id:04d}",
        "name": project.name,
        "root_path": project.root_path,
        "mode": project.mode,
        "docs_entrypoint": project.docs_entrypoint,
    })


def manifest_matches(workspace_root: Path, project: Project) -> bool:
    try:
        manifest = yaml.safe_load(project_manifest_path(workspace_root, project.id).read_text(encoding="utf-8"))
        return manifest.get("project_id") == f"P{project.id:04d}" and manifest.get("root_path") == project.root_path
    except (OSError, yaml.YAMLError, AttributeError):
        return False


def provision_task(workspace_root: Path, task: Task) -> None:
    path = task_manifest_path(workspace_root, task)
    write_yaml_atomic(path, {"format": "rosc.task.v1", "task_id": f"T{task.id:04d}", "project_id": f"P{task.project_id:04d}", "status": task.status})


def task_manifest_path(workspace_root: Path, task: Task) -> Path:
    return project_directory(workspace_root, task.project_id) / "tasks" / f"T{task.id:04d}" / "task.yaml"


def task_manifest_matches(workspace_root: Path, task: Task) -> bool:
    try:
        manifest = yaml.safe_load(task_manifest_path(workspace_root, task).read_text(encoding="utf-8"))
        return manifest.get("task_id") == f"T{task.id:04d}" and manifest.get("project_id") == f"P{task.project_id:04d}"
    except (OSError, yaml.YAMLError, AttributeError):
        return False
