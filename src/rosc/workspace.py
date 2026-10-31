from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .models import Project, Task

MAX_MANIFEST_BYTES = 1_048_576

# Inspection outcomes. "ready" means the file equals the canonical projection exactly.
READY, MISSING, CORRUPT, DRIFT, FOREIGN, UNSAFE = "ready", "missing", "corrupt", "drift", "foreign", "unsafe"


class WorkspaceConflict(Exception):
    """A pre-existing manifest (or path) must not be replaced automatically."""

    def __init__(self, problem: str, message: str) -> None:
        super().__init__(message)
        self.problem = problem
        self.message = message


class WorkspaceError(Exception):
    """A repair step failed; the original manifest bytes are intact or were restored."""


@dataclass
class Inspection:
    problem: str
    raw: bytes | None = None

    @property
    def sha256(self) -> str | None:
        return hashlib.sha256(self.raw).hexdigest() if self.raw is not None else None


def project_directory(workspace_root: Path, project_id: int) -> Path:
    return workspace_root / "projects" / f"P{project_id:04d}"


def project_manifest_path(workspace_root: Path, project_id: int) -> Path:
    return project_directory(workspace_root, project_id) / "project.yaml"


def task_manifest_path(workspace_root: Path, task: Task) -> Path:
    return project_directory(workspace_root, task.project_id) / "tasks" / f"T{task.id:04d}" / "task.yaml"


def project_payload(project: Project) -> dict:
    return {
        "format": "rosc.project.v1",
        "project_id": f"P{project.id:04d}",
        "name": project.name,
        "root_path": project.root_path,
        "mode": project.mode,
        "docs_entrypoint": project.docs_entrypoint,
    }


def task_payload(task: Task) -> dict:
    return {"format": "rosc.task.v1", "task_id": f"T{task.id:04d}", "project_id": f"P{task.project_id:04d}", "status": task.status}


PROJECT_IDENTITY = ("project_id", "root_path")
TASK_IDENTITY = ("task_id", "project_id")


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_directory(path: Path) -> None:
    """Create a ROSC-owned directory, refusing a symlink or non-directory in its place."""
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise WorkspaceConflict(UNSAFE, f"Workspace path {path.name} is a symlink or not a directory")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise WorkspaceConflict(UNSAFE, f"Workspace path {path.name} is a symlink or not a directory")


def _chain_is_unsafe(path: Path, workspace_root: Path) -> bool:
    current = path.parent
    while True:
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            return True
        if current == workspace_root or current == current.parent:
            return False
        current = current.parent


def inspect_manifest(path: Path, expected: dict, identity: tuple[str, ...], workspace_root: Path) -> Inspection:
    if _chain_is_unsafe(path, workspace_root):
        return Inspection(UNSAFE)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return Inspection(MISSING)
    except OSError:
        return Inspection(UNSAFE)
    if not stat.S_ISREG(info.st_mode):
        return Inspection(UNSAFE)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
    except OSError:
        return Inspection(UNSAFE)
    if len(raw) > MAX_MANIFEST_BYTES:
        return Inspection(CORRUPT, raw)
    try:
        loaded = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return Inspection(CORRUPT, raw)
    if not isinstance(loaded, dict) or any(key not in loaded for key in identity):
        return Inspection(CORRUPT, raw)
    if loaded == expected:
        return Inspection(READY, raw)
    if any(loaded[key] != expected[key] for key in identity):
        return Inspection(FOREIGN, raw)
    return Inspection(DRIFT, raw)


def inspect_project(workspace_root: Path, project: Project) -> Inspection:
    return inspect_manifest(project_manifest_path(workspace_root, project.id), project_payload(project), PROJECT_IDENTITY, workspace_root)


def inspect_task(workspace_root: Path, task: Task) -> Inspection:
    return inspect_manifest(task_manifest_path(workspace_root, task), task_payload(task), TASK_IDENTITY, workspace_root)


def _dump(payload: dict) -> bytes:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode("utf-8")


def _temp_file(directory: Path, data: bytes) -> str:
    fd, name = tempfile.mkstemp(prefix=".rosc-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        os.unlink(name)
        raise
    return name


def write_yaml_noclobber(path: Path, payload: dict) -> None:
    """Publish a complete manifest only if nothing exists at path (atomic hard-link, no replace)."""
    name = _temp_file(path.parent, _dump(payload))
    try:
        os.link(name, path)  # raises FileExistsError if a manifest appeared meanwhile
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _atomic_replace(source: str, target: Path) -> None:
    os.replace(source, target)
    _fsync_directory(target.parent)


def _provision(path: Path, payload: dict, identity: tuple[str, ...], workspace_root: Path) -> None:
    current = inspect_manifest(path, payload, identity, workspace_root)
    if current.problem == READY:
        return
    if current.problem != MISSING:
        raise WorkspaceConflict(current.problem, f"Existing manifest is {current.problem}; it was left untouched")
    try:
        write_yaml_noclobber(path, payload)
    except FileExistsError:
        raced = inspect_manifest(path, payload, identity, workspace_root)
        if raced.problem != READY:
            raise WorkspaceConflict(raced.problem, f"Existing manifest is {raced.problem}; it was left untouched") from None


def provision_project(workspace_root: Path, project: Project) -> None:
    directory = project_directory(workspace_root, project.id)
    for part in (workspace_root, workspace_root / "projects", directory, directory / "architect", directory / "tasks"):
        _ensure_directory(part)
    _provision(project_manifest_path(workspace_root, project.id), project_payload(project), PROJECT_IDENTITY, workspace_root)


def provision_task(workspace_root: Path, task: Task) -> None:
    path = task_manifest_path(workspace_root, task)
    _ensure_directory(path.parent.parent)
    _ensure_directory(path.parent)
    _provision(path, task_payload(task), TASK_IDENTITY, workspace_root)


def quarantine_directory(workspace_root: Path, kind: str, record_id: str) -> Path:
    return workspace_root / "quarantine" / kind / record_id


def _write_exclusive(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _write_backup(workspace_root: Path, kind: str, record_id: str, raw: bytes, problem: str) -> dict:
    directory = quarantine_directory(workspace_root, kind, record_id)
    for part in (workspace_root, workspace_root / "quarantine", workspace_root / "quarantine" / kind, directory):
        _ensure_directory(part)
    digest = hashlib.sha256(raw).hexdigest()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_id = f"{record_id}-{stamp}-{digest[:12]}-{uuid.uuid4().hex[:8]}"
    backup = directory / f"{backup_id}.bak"
    _write_exclusive(backup, raw)
    if backup.read_bytes() != raw:
        raise WorkspaceError("Backup verification failed; the original manifest was not replaced")
    meta = {"backup_id": backup_id, "record_id": record_id, "kind": kind, "problem": problem, "sha256": digest,
            "size": len(raw), "created_at": stamp}
    _write_exclusive(directory / f"{backup_id}.json", json.dumps(meta, sort_keys=True).encode("utf-8"))
    _fsync_directory(directory)
    return {"backup_id": backup_id, "backup_path": str(backup.relative_to(workspace_root)), "sha256": digest, "problem": problem}


def _repair(path: Path, payload: dict, identity: tuple[str, ...], workspace_root: Path, kind: str, record_id: str) -> dict | None:
    """Replace a corrupt or drifted manifest after preserving its exact bytes. Fails closed otherwise."""
    current = inspect_manifest(path, payload, identity, workspace_root)
    if current.problem == READY:
        return None
    if current.problem == MISSING:
        _provision(path, payload, identity, workspace_root)
        return None
    if current.problem not in (CORRUPT, DRIFT):
        raise WorkspaceConflict(current.problem, f"Manifest is {current.problem}; manual resolution is required and it was left untouched")
    record = _write_backup(workspace_root, kind, record_id, current.raw, current.problem)
    # The file must still be exactly what was backed up before we replace it.
    again = inspect_manifest(path, payload, identity, workspace_root)
    if again.raw != current.raw:
        raise WorkspaceConflict(again.problem, "Manifest changed during repair; it was left untouched")
    temporary = _temp_file(path.parent, _dump(payload))
    try:
        _atomic_replace(temporary, path)
    except BaseException as exc:
        raise WorkspaceError("Manifest replacement failed; the original is intact") from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if inspect_manifest(path, payload, identity, workspace_root).problem != READY:
        restore = _temp_file(path.parent, current.raw)
        try:
            _atomic_replace(restore, path)
        finally:
            if os.path.exists(restore):
                os.unlink(restore)
        raise WorkspaceError("Repaired manifest failed verification; the original bytes were restored")
    return record


def repair_project(workspace_root: Path, project: Project) -> dict | None:
    return _repair(project_manifest_path(workspace_root, project.id), project_payload(project), PROJECT_IDENTITY,
                   workspace_root, "projects", f"P{project.id:04d}")


def repair_task(workspace_root: Path, task: Task) -> dict | None:
    return _repair(task_manifest_path(workspace_root, task), task_payload(task), TASK_IDENTITY,
                   workspace_root, "tasks", f"T{task.id:04d}")
