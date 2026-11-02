"""Private project-scoped Architect working-document store.

The stable ``doc_id`` selects immutable physical revisions below the ROSC-owned
Workspace. ``logical_path`` is independent metadata for an Architect-managed
documentation tree and never redirects filesystem access.

``document.json`` is the commit point. Failed creates and updates leave
explicit ownership markers. They are reported as diagnostics and require an
authorized reconciliation that preserves candidate bytes in quarantine.
Unknown entries are never deleted or adopted by filename alone.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from .models import Project
from .workspace import (READY, ReplaceNotDurable, WorkspaceConflict, _atomic_replace, _chain_is_unsafe,
                        _ensure_directory, _fsync_directory, _temp_file, inspect_project, project_directory)

FORMAT = "rosc.architect-document.v1"
CREATE_FORMAT = "rosc.architect-create.v1"
PENDING_FORMAT = "rosc.architect-pending.v1"
MAX_DOCUMENT_BYTES = 262_144
MAX_METADATA_BYTES = 262_144
MAX_REVISIONS = 1000
MAX_DOCUMENTS = 500
MAX_LOGICAL_PATH = 512
MEDIA_TYPES = {"text/markdown": ".md", "text/plain": ".txt"}

ARCHITECT_OWNED = "architect_owned"
EXTERNAL_CANONICAL = "external_canonical"
GOVERNANCE_REFERENCE = "governance_reference_snapshot"
CLASSIFICATIONS = (ARCHITECT_OWNED, EXTERNAL_CANONICAL, GOVERNANCE_REFERENCE)
SOURCE_LOCAL = "local_authored"

_DOC_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}", re.ASCII)
_PATH_PART = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}", re.ASCII)
_SHA = re.compile(r"[0-9a-f]{64}", re.ASCII)
_REVISION_FILE = re.compile(r"r([0-9]{4,})\.(?:md|txt)", re.ASCII)
_OP_ID = re.compile(r"[0-9a-f]{32}", re.ASCII)
_CREATE_MARKER = ".creation.json"
_PENDING_MARKER = ".pending.json"
_CREATE_STAGE = re.compile(r"\.create-([0-9a-f]{32})", re.ASCII)


def _rename_noreplace(source: Path, target: Path) -> None:
    """Atomically publish a directory without replacing a raced foreign target."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise UnsafeStore("Atomic no-replace directory publication is unavailable")
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:  # AT_FDCWD, RENAME_NOREPLACE
        problem = ctypes.get_errno()
        if problem == errno.EEXIST:
            raise FileExistsError(problem, os.strerror(problem), str(target))
        raise OSError(problem, os.strerror(problem), str(target))


class StoreError(Exception):
    """Base class; store failures are fail-closed."""


class InvalidDocument(StoreError):
    """Identity, path, type, size or encoding is outside the contract."""


class DocumentNotFound(StoreError):
    pass


class DocumentExists(StoreError):
    pass


class RevisionConflict(StoreError):
    def __init__(self, message: str, current_revision: int | None) -> None:
        super().__init__(message)
        self.current_revision = current_revision


class UnsafeStore(StoreError):
    """Symlink, foreign identity, unexpected entry, tampering or invalid metadata."""


class AuthorizationDenied(StoreError):
    pass


class CommitUncertain(StoreError):
    """The operation crossed a replace boundary but directory durability is unknown."""

    def __init__(self, message: str, current_revision: int | None, committed: bool) -> None:
        super().__init__(message)
        self.current_revision = current_revision
        self.committed = committed


@dataclass(frozen=True)
class ActorContext:
    """Trusted actor slot; ``actor_id`` is provenance, not authentication."""

    actor_id: str


class AuthorizationPolicy(Protocol):
    def authorize(self, actor: ActorContext, action: str, project_id: int, doc_id: str | None) -> bool: ...


@dataclass(frozen=True)
class RevisionInfo:
    revision: int
    sha256: str
    size: int
    created_at: str
    actor_id: str
    file: str


@dataclass(frozen=True)
class DocumentInfo:
    project_id: str
    doc_id: str
    logical_path: str
    classification: str
    source: dict
    media_type: str
    revision: int
    sha256: str
    size: int
    created_at: str
    updated_at: str
    revisions: tuple[RevisionInfo, ...]


@dataclass(frozen=True)
class StoreDiagnostic:
    diagnostic_id: str
    code: str
    entry: str
    message: str
    doc_id: str | None = None
    logical_path: str | None = None
    recoverable: bool = False


@dataclass(frozen=True)
class RecoveryResult:
    diagnostic_id: str
    outcome: str
    quarantine_path: str
    doc_id: str
    current_revision: int | None


@dataclass(frozen=True)
class Document:
    info: DocumentInfo
    content: bytes
    revision: int

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")


def normalize_doc_id(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidDocument("Document id must be text")
    doc_id = value.strip().lower()
    if not _DOC_ID.fullmatch(doc_id) or doc_id.strip(".") == "" or doc_id.endswith("."):
        raise InvalidDocument("Document id must be 1-64 characters of a-z, 0-9, '.', '_' or '-', starting with a letter or digit")
    return doc_id


def normalize_logical_path(value: str) -> str:
    """Return a canonical project-relative metadata path; never a filesystem path."""
    if not isinstance(value, str):
        raise InvalidDocument("Logical path must be text")
    if "\\" in value or "\x00" in value:
        raise InvalidDocument("Logical path must use forward slashes")
    path = value.strip().lower()
    if not path or len(path) > MAX_LOGICAL_PATH or path.startswith("/") or path.endswith("/") or "//" in path:
        raise InvalidDocument("Logical path must be a normalized non-empty relative path")
    parts = path.split("/")
    if any(part in ("", ".", "..") or not _PATH_PART.fullmatch(part) or part.endswith(".") for part in parts):
        raise InvalidDocument("Logical path contains an empty, ambiguous or unsupported component")
    return "/".join(parts)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _project_label(project_id: int) -> str:
    return f"P{project_id:04d}"


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


class ArchitectDocumentStore:
    """Filesystem layer. The service checks Project existence/readiness and policy."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root

    def _documents_root(self, project_id: int) -> Path:
        return project_directory(self.workspace_root, project_id) / "architect" / "documents"

    def _quarantine_root(self, project_id: int) -> Path:
        return project_directory(self.workspace_root, project_id) / "architect" / "quarantine"

    def _prepare_root(self, project_id: int) -> Path:
        base = project_directory(self.workspace_root, project_id)
        for part in (self.workspace_root, self.workspace_root / "projects", base, base / "architect", self._documents_root(project_id)):
            self._ensure(part)
        return self._documents_root(project_id)

    @staticmethod
    def _ensure(path: Path) -> None:
        try:
            _ensure_directory(path)
        except WorkspaceConflict as exc:
            raise UnsafeStore(exc.message) from exc

    def _existing_root(self, project_id: int) -> Path | None:
        root = self._documents_root(project_id)
        if _chain_is_unsafe(root / "x", self.workspace_root):
            raise UnsafeStore("Architect document path contains a symlink or non-directory")
        if not root.exists():
            return None
        if root.is_symlink() or not root.is_dir():
            raise UnsafeStore("Architect document root is a symlink or not a directory")
        return root

    @staticmethod
    def _read_regular(path: Path, limit: int) -> bytes:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise UnsafeStore("Architect document file is not accessible") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise UnsafeStore("Architect document file is not a regular file within the size limit")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as handle:
                data = handle.read(limit + 1)
        except OSError as exc:
            raise UnsafeStore("Architect document file is not accessible") from exc
        if len(data) > limit:
            raise UnsafeStore("Architect document file is not a regular file within the size limit")
        return data

    def _load_json(self, path: Path) -> dict:
        try:
            value = json.loads(self._read_regular(path, MAX_METADATA_BYTES).decode("utf-8"))
        except FileNotFoundError:
            raise
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise UnsafeStore("Architect ownership metadata is invalid") from exc
        if not isinstance(value, dict):
            raise UnsafeStore("Architect ownership metadata is invalid")
        return value

    @staticmethod
    def _valid_sha(value: object) -> bool:
        return isinstance(value, str) and _SHA.fullmatch(value) is not None

    @staticmethod
    def _valid_int(value: object, minimum: int, maximum: int | None = None) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= minimum and (maximum is None or value <= maximum)

    def _load_meta(self, project_id: int, directory: Path, doc_id: str) -> dict:
        if directory.is_symlink() or not directory.is_dir():
            raise UnsafeStore("Architect document directory is a symlink or not a directory")
        try:
            meta = self._load_json(directory / "document.json")
        except FileNotFoundError:
            raise UnsafeStore("Architect document has no committed metadata") from None
        required = {"format", "project_id", "doc_id", "classification", "source", "media_type", "revision",
                    "created_at", "updated_at", "revisions"}
        source = meta.get("source")
        revisions = meta.get("revisions")
        if (set(meta) not in (required, required | {"logical_path"}) or meta.get("format") != FORMAT or
                meta.get("project_id") != _project_label(project_id) or meta.get("doc_id") != doc_id or
                meta.get("classification") != ARCHITECT_OWNED or meta.get("media_type") not in MEDIA_TYPES or
                not isinstance(source, dict) or set(source) != {"kind", "external_id", "external_revision", "verified_at"} or
                source.get("kind") != SOURCE_LOCAL or source.get("external_id") is not None or
                source.get("external_revision") is not None or source.get("verified_at") is not None or
                not isinstance(revisions, list) or not 1 <= len(revisions) <= MAX_REVISIONS or
                not self._valid_int(meta.get("revision"), 1) or meta["revision"] != len(revisions) or
                not isinstance(meta.get("created_at"), str) or not isinstance(meta.get("updated_at"), str)):
            raise UnsafeStore("Architect document metadata is invalid, foreign or from another Project")
        if "logical_path" in meta:
            try:
                if normalize_logical_path(meta["logical_path"]) != meta["logical_path"]:
                    raise InvalidDocument("not canonical")
            except InvalidDocument as exc:
                raise UnsafeStore("Architect document logical path is invalid") from exc
        for number, entry in enumerate(revisions, start=1):
            if (not isinstance(entry, dict) or set(entry) != {"revision", "sha256", "size", "created_at", "actor_id", "file"} or
                    entry.get("revision") != number or not self._valid_sha(entry.get("sha256")) or
                    not self._valid_int(entry.get("size"), 1, MAX_DOCUMENT_BYTES) or
                    entry.get("file") != f"r{number:04d}{MEDIA_TYPES[meta['media_type']]}" or
                    not isinstance(entry.get("created_at"), str) or not isinstance(entry.get("actor_id"), str)):
                raise UnsafeStore("Architect document metadata is invalid, foreign or from another Project")
        return meta

    @staticmethod
    def _info(meta: dict) -> DocumentInfo:
        revisions = tuple(RevisionInfo(**{key: entry[key] for key in ("revision", "sha256", "size", "created_at", "actor_id", "file")})
                          for entry in meta["revisions"])
        head = revisions[-1]
        return DocumentInfo(project_id=meta["project_id"], doc_id=meta["doc_id"],
                            logical_path=meta.get("logical_path", meta["doc_id"]), classification=meta["classification"],
                            source=dict(meta["source"]), media_type=meta["media_type"], revision=head.revision,
                            sha256=head.sha256, size=head.size, created_at=meta["created_at"],
                            updated_at=meta["updated_at"], revisions=revisions)

    def _read_revision(self, directory: Path, entry: RevisionInfo) -> bytes:
        try:
            data = self._read_regular(directory / entry.file, MAX_DOCUMENT_BYTES)
        except FileNotFoundError:
            raise UnsafeStore("Architect document revision file is missing") from None
        if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
            raise UnsafeStore("Architect document revision does not match its recorded digest")
        return data

    def _load_creation(self, project_id: int, directory: Path, doc_id: str) -> dict:
        marker = self._load_json(directory / _CREATE_MARKER)
        if (set(marker) != {"format", "project_id", "doc_id", "logical_path", "media_type", "sha256", "size", "actor_id",
                            "created_at", "op_id"} or marker.get("format") != CREATE_FORMAT or
                marker.get("project_id") != _project_label(project_id) or marker.get("doc_id") != doc_id or
                marker.get("media_type") not in MEDIA_TYPES or not self._valid_sha(marker.get("sha256")) or
                not self._valid_int(marker.get("size"), 1, MAX_DOCUMENT_BYTES) or not isinstance(marker.get("actor_id"), str) or
                not isinstance(marker.get("created_at"), str) or not isinstance(marker.get("op_id"), str) or
                _OP_ID.fullmatch(marker["op_id"]) is None):
            raise UnsafeStore("Creation marker is invalid or foreign")
        try:
            if normalize_logical_path(marker["logical_path"]) != marker["logical_path"]:
                raise InvalidDocument("not canonical")
        except InvalidDocument as exc:
            raise UnsafeStore("Creation marker logical path is invalid") from exc
        return marker

    def _load_pending(self, project_id: int, directory: Path, doc_id: str) -> dict:
        marker = self._load_json(directory / _PENDING_MARKER)
        match = _REVISION_FILE.fullmatch(marker.get("file", "")) if isinstance(marker.get("file"), str) else None
        if (set(marker) != {"format", "project_id", "doc_id", "base_revision", "candidate_revision", "file", "sha256",
                            "size", "actor_id", "created_at", "op_id"} or marker.get("format") != PENDING_FORMAT or
                marker.get("project_id") != _project_label(project_id) or marker.get("doc_id") != doc_id or
                not self._valid_int(marker.get("base_revision"), 1) or marker.get("candidate_revision") != marker["base_revision"] + 1 or
                match is None or int(match.group(1)) != marker["candidate_revision"] or not self._valid_sha(marker.get("sha256")) or
                not self._valid_int(marker.get("size"), 1, MAX_DOCUMENT_BYTES) or not isinstance(marker.get("actor_id"), str) or
                not isinstance(marker.get("created_at"), str) or not isinstance(marker.get("op_id"), str) or
                _OP_ID.fullmatch(marker["op_id"]) is None):
            raise UnsafeStore("Pending revision marker is invalid or foreign")
        return marker

    def _candidate_matches(self, directory: Path, marker: dict) -> bool:
        try:
            data = self._read_regular(directory / marker["file"], MAX_DOCUMENT_BYTES)
        except (FileNotFoundError, UnsafeStore):
            return False
        return len(data) == marker["size"] and hashlib.sha256(data).hexdigest() == marker["sha256"]

    def _creation_diagnostic(self, project_id: int, directory: Path, doc_id: str) -> StoreDiagnostic:
        try:
            marker = self._load_creation(project_id, directory, doc_id)
            revision_name = f"r0001{MEDIA_TYPES[marker['media_type']]}"
            allowed = {_CREATE_MARKER, "document.json", revision_name}
            names = {entry.name for entry in directory.iterdir()}
            revision = directory / revision_name
            revision_ok = not revision.exists() or self._candidate_matches(directory, {**marker, "file": revision.name})
            safe = names <= allowed and revision_ok
            return StoreDiagnostic(f"creation:{doc_id}:{marker['op_id']}", "incomplete_creation" if safe else "unsafe_creation",
                                   doc_id, "ROSC-owned initial creation is incomplete" if safe else
                                   "Creation marker exists but contents are unexpected; automatic recovery is refused",
                                   doc_id, marker["logical_path"], safe)
        except (FileNotFoundError, UnsafeStore, OSError):
            return StoreDiagnostic(f"unsafe:{doc_id}", "unknown_document", doc_id,
                                   "Directory has no valid committed metadata or ROSC ownership proof", doc_id, None, False)

    def _document_diagnostics(self, project_id: int, directory: Path, info: DocumentInfo) -> list[StoreDiagnostic]:
        allowed = {"document.json", *(entry.file for entry in info.revisions)}
        diagnostics: list[StoreDiagnostic] = []
        handled: set[str] = set()
        if os.path.lexists(directory / _CREATE_MARKER):
            try:
                marker = self._load_creation(project_id, directory, info.doc_id)
                diagnostics.append(StoreDiagnostic(f"creation:{info.doc_id}:{marker['op_id']}", "committed_creation_marker",
                                                   info.doc_id, "Document committed but its creation marker still needs reconciliation",
                                                   info.doc_id, info.logical_path, True))
            except UnsafeStore:
                diagnostics.append(StoreDiagnostic(f"unsafe:{info.doc_id}:{_CREATE_MARKER}", "foreign_entry", info.doc_id,
                                                   "Invalid creation marker was preserved", info.doc_id, info.logical_path, False))
            handled.add(_CREATE_MARKER)
        if os.path.lexists(directory / _PENDING_MARKER):
            try:
                marker = self._load_pending(project_id, directory, info.doc_id)
                matches = self._candidate_matches(directory, marker)
                committed = (info.revision == marker["candidate_revision"] and info.revisions[-1].file == marker["file"] and
                             info.sha256 == marker["sha256"])
                uncommitted = info.revision == marker["base_revision"] and matches
                recoverable = committed or uncommitted
                code = "commit_uncertain" if committed else "pending_revision" if uncommitted else "unsafe_pending"
                diagnostics.append(StoreDiagnostic(f"pending:{info.doc_id}:{marker['op_id']}", code, info.doc_id,
                                                   "Pending revision requires explicit reconciliation" if recoverable else
                                                   "Pending marker or candidate bytes conflict; automatic recovery is refused",
                                                   info.doc_id, info.logical_path, recoverable))
                if matches:
                    handled.add(marker["file"])
            except UnsafeStore:
                diagnostics.append(StoreDiagnostic(f"unsafe:{info.doc_id}:{_PENDING_MARKER}", "foreign_entry", info.doc_id,
                                                   "Invalid pending marker was preserved", info.doc_id, info.logical_path, False))
            handled.add(_PENDING_MARKER)
        for child in directory.iterdir():
            if child.name not in allowed and child.name not in handled:
                try:
                    mode = os.lstat(child).st_mode
                    recoverable = stat.S_ISREG(mode) or stat.S_ISLNK(mode)
                except OSError:
                    recoverable = False
                diagnostics.append(StoreDiagnostic(f"unsafe:{info.doc_id}:{child.name}", "foreign_entry", info.doc_id,
                                                   f"Unexpected entry {child.name!r} was preserved", info.doc_id,
                                                   info.logical_path, recoverable))
        return diagnostics

    def list(self, project_id: int) -> list[DocumentInfo | StoreDiagnostic]:
        root = self._existing_root(project_id)
        if root is None:
            return []
        documents: list[DocumentInfo] = []
        diagnostics: list[StoreDiagnostic] = []
        for child in sorted(root.iterdir(), key=lambda path: path.name):
            if child.name == ".lock":
                continue
            stage_match = _CREATE_STAGE.fullmatch(child.name)
            if stage_match and not child.is_symlink() and child.is_dir():
                try:
                    marker = self._load_json(child / _CREATE_MARKER)
                    doc_id = normalize_doc_id(marker.get("doc_id"))
                    if marker.get("op_id") != stage_match.group(1):
                        raise UnsafeStore("Creation stage identity does not match its marker")
                    diagnostics.append(self._creation_diagnostic(project_id, child, doc_id))
                except (FileNotFoundError, InvalidDocument, UnsafeStore):
                    diagnostics.append(StoreDiagnostic(f"unsafe-root:{child.name}", "foreign_root_entry", child.name,
                                                       "Unexpected root entry was preserved", None, None, False))
                continue
            if child.name.startswith(".") or not _DOC_ID.fullmatch(child.name) or child.is_symlink() or not child.is_dir():
                diagnostics.append(StoreDiagnostic(f"unsafe-root:{child.name}", "foreign_root_entry", child.name,
                                                   "Unexpected root entry was preserved", None, None, False))
                continue
            try:
                info = self._info(self._load_meta(project_id, child, child.name))
                self._read_revision(child, info.revisions[-1])
            except UnsafeStore:
                diagnostics.append(self._creation_diagnostic(project_id, child, child.name))
                continue
            documents.append(info)
            diagnostics.extend(self._document_diagnostics(project_id, child, info))
        return [*documents, *diagnostics]

    def read(self, project_id: int, doc_id: str, revision: int | None = None) -> Document:
        doc_id = normalize_doc_id(doc_id)
        root = self._existing_root(project_id)
        if root is None or not os.path.lexists(root / doc_id):
            raise DocumentNotFound(f"Document {doc_id} does not exist")
        directory = root / doc_id
        info = self._info(self._load_meta(project_id, directory, doc_id))
        number = info.revision if revision is None else revision
        if not self._valid_int(number, 1, info.revision):
            raise DocumentNotFound(f"Document {doc_id} has no revision {revision}")
        return Document(info, self._read_revision(directory, info.revisions[number - 1]), number)

    @staticmethod
    def _write_exclusive(path: Path, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def _logical_paths(self, project_id: int, root: Path, exclude: str | None = None) -> dict[str, str]:
        result: dict[str, str] = {}
        for child in root.iterdir():
            stage_match = _CREATE_STAGE.fullmatch(child.name)
            if stage_match and not child.is_symlink() and child.is_dir():
                try:
                    marker = self._load_creation(project_id, child, normalize_doc_id(self._load_json(child / _CREATE_MARKER).get("doc_id")))
                    if marker["op_id"] == stage_match.group(1):
                        result[marker["logical_path"]] = marker["doc_id"]
                except (FileNotFoundError, InvalidDocument, UnsafeStore):
                    pass
                continue
            if child.name.startswith(".") or child.name == exclude or not _DOC_ID.fullmatch(child.name) or child.is_symlink() or not child.is_dir():
                continue
            try:
                info = self._info(self._load_meta(project_id, child, child.name))
                result[info.logical_path] = child.name
            except UnsafeStore:
                try:
                    marker = self._load_creation(project_id, child, child.name)
                    result[marker["logical_path"]] = child.name
                except (FileNotFoundError, UnsafeStore):
                    continue
        return result

    def write(self, project_id: int, doc_id: str, content: str | bytes, *, actor_id: str, expected_revision: int | None = None,
              media_type: str = "text/markdown", logical_path: str | None = None) -> DocumentInfo:
        doc_id = normalize_doc_id(doc_id)
        if media_type not in MEDIA_TYPES:
            raise InvalidDocument("Unsupported media type")
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        if not data or len(data) > MAX_DOCUMENT_BYTES:
            raise InvalidDocument(f"Document must be 1..{MAX_DOCUMENT_BYTES} bytes")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidDocument("Document must be valid UTF-8 text") from exc
        if b"\x00" in data:
            raise InvalidDocument("Document must be text")
        if expected_revision is not None and not self._valid_int(expected_revision, 1):
            raise InvalidDocument("expected_revision must be a positive integer")
        normalized_path = normalize_logical_path(logical_path) if logical_path is not None else None

        root = self._prepare_root(project_id)
        try:
            lock_fd = os.open(root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise UnsafeStore("Architect document lock is unsafe or inaccessible") from exc
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            return self._write_locked(project_id, root, doc_id, data, str(actor_id), expected_revision, media_type, normalized_path)
        finally:
            os.close(lock_fd)

    @staticmethod
    def _publish_meta(directory: Path, meta: dict) -> None:
        temporary = _temp_file(directory, _json_bytes(meta))
        try:
            _atomic_replace(temporary, directory / "document.json")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _observed_revision(self, project_id: int, directory: Path, doc_id: str, expected_sha: str) -> tuple[int | None, bool]:
        try:
            info = self._info(self._load_meta(project_id, directory, doc_id))
            return info.revision, info.sha256 == expected_sha
        except UnsafeStore:
            return None, False

    def _write_locked(self, project_id: int, root: Path, doc_id: str, data: bytes, actor_id: str, expected: int | None,
                      media_type: str, logical_path: str | None) -> DocumentInfo:
        directory = root / doc_id
        stamp = _now()
        digest = hashlib.sha256(data).hexdigest()
        creating = not os.path.lexists(directory)
        if creating:
            logical_path = logical_path or doc_id
            if expected is not None:
                raise RevisionConflict(f"Document {doc_id} does not exist", None)
            if sum(1 for child in root.iterdir()
                   if not child.name.startswith(".") or _CREATE_STAGE.fullmatch(child.name)) >= MAX_DOCUMENTS:
                raise InvalidDocument("Document count limit reached")
            owner = self._logical_paths(project_id, root).get(logical_path)
            if owner is not None:
                raise DocumentExists(f"Logical path {logical_path} is already used by {owner}")
            op_id = uuid.uuid4().hex
            stage = root / f".create-{op_id}"
            os.mkdir(stage, 0o700)
            directory = stage
            marker = {"format": CREATE_FORMAT, "project_id": _project_label(project_id), "doc_id": doc_id,
                      "logical_path": logical_path, "media_type": media_type, "sha256": digest, "size": len(data),
                      "actor_id": actor_id, "created_at": stamp, "op_id": op_id}
            self._write_exclusive(directory / _CREATE_MARKER, _json_bytes(marker))
            _fsync_directory(directory)
            _fsync_directory(root)
            number = 1
            meta = {"format": FORMAT, "project_id": _project_label(project_id), "doc_id": doc_id,
                    "logical_path": logical_path, "classification": ARCHITECT_OWNED,
                    "source": {"kind": SOURCE_LOCAL, "external_id": None, "external_revision": None, "verified_at": None},
                    "media_type": media_type, "created_at": stamp, "revisions": []}
        else:
            previous = self._load_meta(project_id, directory, doc_id)
            info = self._info(previous)
            logical_path = logical_path or info.logical_path
            self._read_revision(directory, info.revisions[-1])
            if self._document_diagnostics(project_id, directory, info):
                raise UnsafeStore(f"Document {doc_id} has preserved entries requiring reconciliation")
            if expected is None:
                raise DocumentExists(f"Document {doc_id} already exists at revision {info.revision}")
            if expected != info.revision:
                raise RevisionConflict(f"Document {doc_id} is at revision {info.revision}, not {expected}", info.revision)
            if previous["media_type"] != media_type:
                raise InvalidDocument("Media type cannot change between revisions")
            if logical_path != info.logical_path:
                raise InvalidDocument("Logical path cannot change during a revision; move/rename is a separate future operation")
            owner = self._logical_paths(project_id, root, exclude=doc_id).get(logical_path)
            if owner is not None:
                raise DocumentExists(f"Logical path {logical_path} is already used by {owner}")
            if len(previous["revisions"]) >= MAX_REVISIONS:
                raise InvalidDocument("Revision limit reached")
            number = info.revision + 1
            meta = dict(previous)
            meta["revisions"] = list(previous["revisions"])
            pending = {"format": PENDING_FORMAT, "project_id": _project_label(project_id), "doc_id": doc_id,
                       "base_revision": info.revision, "candidate_revision": number,
                       "file": f"r{number:04d}{MEDIA_TYPES[media_type]}", "sha256": digest, "size": len(data),
                       "actor_id": actor_id, "created_at": stamp, "op_id": uuid.uuid4().hex}
            self._write_exclusive(directory / _PENDING_MARKER, _json_bytes(pending))
            _fsync_directory(directory)
        file_name = f"r{number:04d}{MEDIA_TYPES[media_type]}"
        target = directory / file_name
        try:
            self._write_exclusive(target, data)
            _fsync_directory(directory)
            meta["revisions"].append({"revision": number, "sha256": digest, "size": len(data), "created_at": stamp,
                                      "actor_id": actor_id, "file": file_name})
            meta["revision"] = number
            meta["updated_at"] = stamp
            self._publish_meta(directory, meta)
        except ReplaceNotDurable as exc:
            observed, committed = (None, False) if creating else self._observed_revision(project_id, directory, doc_id, digest)
            raise CommitUncertain("Metadata replace succeeded but directory durability is unknown; reconcile before retrying",
                                  observed, committed) from exc
        if creating:
            final_directory = root / doc_id
            try:
                _rename_noreplace(directory, final_directory)
                directory = final_directory
                _fsync_directory(root)
            except FileExistsError as exc:
                raise DocumentExists(f"Document {doc_id} appeared during publication; staged bytes require reconciliation") from exc
            except OSError as exc:
                if os.path.lexists(final_directory) and not os.path.lexists(root / f".create-{marker['op_id']}"):
                    observed, committed = self._observed_revision(project_id, final_directory, doc_id, digest)
                    raise CommitUncertain("Initial publication landed but root-directory durability is unknown; reconcile before retrying",
                                          observed, committed) from exc
                raise
        marker_path = directory / (_CREATE_MARKER if creating else _PENDING_MARKER)
        try:
            marker_path.unlink()
            _fsync_directory(directory)
        except OSError as exc:
            observed, committed = self._observed_revision(project_id, directory, doc_id, digest)
            raise CommitUncertain("Revision committed but ownership-marker cleanup is not durable; reconcile before retrying",
                                  observed, committed) from exc
        return self._info(meta)

    def reconcile(self, project_id: int, diagnostic_id: str) -> RecoveryResult:
        root = self._prepare_root(project_id)
        try:
            lock_fd = os.open(root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise UnsafeStore("Architect document lock is unsafe or inaccessible") from exc
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if diagnostic_id.startswith("creation:"):
                return self._reconcile_creation(project_id, root, diagnostic_id)
            if diagnostic_id.startswith("pending:"):
                return self._reconcile_pending(project_id, root, diagnostic_id)
            if diagnostic_id.startswith("unsafe:"):
                return self._reconcile_unknown(project_id, root, diagnostic_id)
            raise UnsafeStore("Diagnostic is not safely recoverable")
        finally:
            os.close(lock_fd)

    def _prepare_quarantine(self, project_id: int, kind: str, doc_id: str) -> Path:
        base = project_directory(self.workspace_root, project_id) / "architect"
        root = self._quarantine_root(project_id)
        target = root / kind / doc_id
        for part in (base, root, root / kind, target):
            self._ensure(part)
        return target

    def _reconcile_creation(self, project_id: int, root: Path, diagnostic_id: str) -> RecoveryResult:
        parts = diagnostic_id.split(":")
        if len(parts) != 3:
            raise UnsafeStore("Invalid creation diagnostic")
        _, raw_doc_id, op_id = parts
        doc_id = normalize_doc_id(raw_doc_id)
        if _OP_ID.fullmatch(op_id) is None:
            raise UnsafeStore("Invalid creation diagnostic")
        stage = root / f".create-{op_id}"
        directory = stage if os.path.lexists(stage) else root / doc_id
        destination_parent = self._prepare_quarantine(project_id, "initial-create", doc_id)
        destination = destination_parent / op_id
        if not os.path.lexists(directory):
            if destination.is_dir() and not destination.is_symlink():
                marker = self._load_creation(project_id, destination, doc_id)
                if marker["op_id"] == op_id:
                    return RecoveryResult(diagnostic_id, "already_quarantined", str(destination.relative_to(self.workspace_root)), doc_id, None)
            raise UnsafeStore("Incomplete creation no longer exists at the recorded location")
        try:
            meta = self._load_meta(project_id, directory, doc_id)
        except UnsafeStore:
            meta = None
        marker_destination = destination_parent / f"{op_id}.creation.json"
        if meta is not None and not os.path.lexists(directory / _CREATE_MARKER) and marker_destination.exists():
            marker = self._load_json(marker_destination)
            if (marker.get("format") == CREATE_FORMAT and marker.get("project_id") == _project_label(project_id) and
                    marker.get("doc_id") == doc_id and marker.get("op_id") == op_id):
                return RecoveryResult(diagnostic_id, "already_quarantined", str(marker_destination.relative_to(self.workspace_root)),
                                      doc_id, self._info(meta).revision)
            raise UnsafeStore("Quarantined creation marker conflicts with the diagnostic")
        marker = self._load_creation(project_id, directory, doc_id)
        if marker["op_id"] != op_id:
            raise UnsafeStore("Creation diagnostic no longer matches the ownership marker")
        if meta is not None:
            info = self._info(meta)
            if marker_destination.exists():
                if self._read_regular(marker_destination, MAX_METADATA_BYTES) != self._read_regular(directory / _CREATE_MARKER, MAX_METADATA_BYTES):
                    raise UnsafeStore("Quarantine destination conflicts with the creation marker")
            else:
                os.rename(directory / _CREATE_MARKER, marker_destination)
            _fsync_directory(directory)
            _fsync_directory(destination_parent)
            return RecoveryResult(diagnostic_id, "committed_marker_quarantined",
                                  str(marker_destination.relative_to(self.workspace_root)), doc_id, info.revision)
        diagnostic = self._creation_diagnostic(project_id, directory, doc_id)
        if diagnostic.diagnostic_id != diagnostic_id or not diagnostic.recoverable:
            raise UnsafeStore("Incomplete creation contents are not proven-owned; they were preserved in place")
        if os.path.lexists(destination):
            raise UnsafeStore("Quarantine destination already exists; incomplete creation was preserved in place")
        os.rename(directory, destination)
        _fsync_directory(root)
        _fsync_directory(destination_parent)
        return RecoveryResult(diagnostic_id, "incomplete_creation_quarantined", str(destination.relative_to(self.workspace_root)), doc_id, None)

    def _reconcile_pending(self, project_id: int, root: Path, diagnostic_id: str) -> RecoveryResult:
        parts = diagnostic_id.split(":")
        if len(parts) != 3:
            raise UnsafeStore("Invalid pending diagnostic")
        _, raw_doc_id, op_id = parts
        doc_id = normalize_doc_id(raw_doc_id)
        if _OP_ID.fullmatch(op_id) is None:
            raise UnsafeStore("Invalid pending diagnostic")
        directory = root / doc_id
        meta = self._load_meta(project_id, directory, doc_id)
        info = self._info(meta)
        destination = self._prepare_quarantine(project_id, "revisions", doc_id) / op_id
        self._ensure(destination)
        marker_source = directory / _PENDING_MARKER
        marker_destination = destination / _PENDING_MARKER
        if marker_source.exists():
            marker = self._load_pending(project_id, directory, doc_id)
        elif marker_destination.exists():
            marker = self._load_json(marker_destination)
            if marker.get("format") != PENDING_FORMAT or marker.get("op_id") != op_id or marker.get("doc_id") != doc_id:
                raise UnsafeStore("Quarantined pending marker conflicts with the diagnostic")
        else:
            raise UnsafeStore("Pending ownership marker is missing")
        if marker.get("op_id") != op_id:
            raise UnsafeStore("Pending diagnostic no longer matches the ownership marker")
        committed = (info.revision == marker["candidate_revision"] and info.sha256 == marker["sha256"] and
                     info.revisions[-1].file == marker["file"])
        uncommitted = info.revision == marker["base_revision"] and self._candidate_matches(directory, marker)
        candidate_source = directory / marker["file"]
        candidate_destination = destination / marker["file"]
        if not committed and not uncommitted:
            if not (info.revision == marker["base_revision"] and candidate_destination.exists() and
                    hashlib.sha256(self._read_regular(candidate_destination, MAX_DOCUMENT_BYTES)).hexdigest() == marker["sha256"]):
                raise UnsafeStore("Pending revision is not in a proven recoverable state; all bytes were preserved")
            uncommitted = True
        if uncommitted and candidate_source.exists():
            if candidate_destination.exists():
                raise UnsafeStore("Quarantine candidate already exists; source bytes were preserved")
            os.rename(candidate_source, candidate_destination)
        if marker_source.exists():
            if marker_destination.exists():
                if self._read_regular(marker_destination, MAX_METADATA_BYTES) != self._read_regular(marker_source, MAX_METADATA_BYTES):
                    raise UnsafeStore("Quarantine marker conflicts; source marker was preserved")
                marker_source.unlink()
            else:
                os.rename(marker_source, marker_destination)
        _fsync_directory(directory)
        _fsync_directory(destination)
        outcome = "committed_marker_quarantined" if committed else "uncommitted_revision_quarantined"
        return RecoveryResult(diagnostic_id, outcome, str(destination.relative_to(self.workspace_root)), doc_id, info.revision)

    def _reconcile_unknown(self, project_id: int, root: Path, diagnostic_id: str) -> RecoveryResult:
        parts = diagnostic_id.split(":", 2)
        if len(parts) != 3:
            raise UnsafeStore("Invalid foreign-entry diagnostic")
        _, raw_doc_id, entry_name = parts
        doc_id = normalize_doc_id(raw_doc_id)
        if (not entry_name or entry_name in (".", "..") or Path(entry_name).name != entry_name or
                "/" in entry_name or "\\" in entry_name or "\x00" in entry_name):
            raise UnsafeStore("Invalid foreign-entry diagnostic")
        directory = root / doc_id
        info = self._info(self._load_meta(project_id, directory, doc_id))
        committed_names = {"document.json", *(entry.file for entry in info.revisions), _CREATE_MARKER, _PENDING_MARKER}
        if entry_name in committed_names:
            raise UnsafeStore("Protected document state cannot be reconciled as an unknown entry")
        source = directory / entry_name
        receipt_id = hashlib.sha256(diagnostic_id.encode("utf-8")).hexdigest()[:24]
        destination = self._prepare_quarantine(project_id, "foreign-entries", doc_id) / receipt_id
        quarantined = destination / entry_name
        if not os.path.lexists(source):
            if os.path.lexists(quarantined):
                return RecoveryResult(diagnostic_id, "already_quarantined", str(quarantined.relative_to(self.workspace_root)),
                                      doc_id, info.revision)
            raise UnsafeStore("Unknown entry no longer exists at the recorded location")
        mode = os.lstat(source).st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            raise UnsafeStore("Unknown entry type is not eligible for bounded reconciliation")
        if os.path.lexists(destination):
            if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
                raise UnsafeStore("Quarantine destination already exists; unknown entry was preserved in place")
        else:
            self._ensure(destination)
        os.rename(source, quarantined)
        _fsync_directory(directory)
        _fsync_directory(destination)
        return RecoveryResult(diagnostic_id, "foreign_entry_quarantined", str(quarantined.relative_to(self.workspace_root)),
                              doc_id, info.revision)


class ArchitectDocumentService:
    """Core entrypoint. Nothing is mounted on an HTTP route."""

    def __init__(self, session_factory: sessionmaker[Session], workspace_root: Path, policy: AuthorizationPolicy) -> None:
        self._sessions = session_factory
        self._root = workspace_root
        self._policy = policy
        self._store = ArchitectDocumentStore(workspace_root)

    def _gate(self, actor: ActorContext, action: str, project_id: int, doc_id: str | None) -> None:
        if not isinstance(actor, ActorContext) or not self._policy.authorize(actor, action, project_id, doc_id):
            raise AuthorizationDenied(f"Not authorized to {action}")
        with self._sessions() as session:
            project = session.get(Project, project_id)
            if project is None:
                raise DocumentNotFound("Project not found")
            if project.workspace_state != "ready" or inspect_project(self._root, project).problem != READY:
                raise UnsafeStore("Project Workspace is not ready")

    def list_documents(self, actor: ActorContext, project_id: int) -> list[DocumentInfo | StoreDiagnostic]:
        self._gate(actor, "list", project_id, None)
        return self._store.list(project_id)

    def read_document(self, actor: ActorContext, project_id: int, doc_id: str, revision: int | None = None) -> Document:
        self._gate(actor, "read", project_id, doc_id)
        return self._store.read(project_id, doc_id, revision)

    def write_document(self, actor: ActorContext, project_id: int, doc_id: str, content: str | bytes, *,
                       expected_revision: int | None = None, media_type: str = "text/markdown",
                       logical_path: str | None = None) -> DocumentInfo:
        self._gate(actor, "write", project_id, doc_id)
        return self._store.write(project_id, doc_id, content, actor_id=actor.actor_id,
                                 expected_revision=expected_revision, media_type=media_type, logical_path=logical_path)

    def reconcile_document(self, actor: ActorContext, project_id: int, diagnostic_id: str) -> RecoveryResult:
        parts = diagnostic_id.split(":", 2)
        doc_id = parts[1] if len(parts) > 1 else None
        self._gate(actor, "reconcile", project_id, doc_id)
        return self._store.reconcile(project_id, diagnostic_id)
