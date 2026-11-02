"""Local, private, project-scoped Architect working-document store (P008, M1-A).

Service-only: nothing here is mounted on an HTTP route. Private bytes live under
``<workspace>/projects/Pxxxx/architect/documents/<doc_id>/`` inside the ROSC-owned Workspace, never in the
source repository or in a connected Project's checkout.

On-disk layout per document::

    document.json   atomic-replaced metadata: identity, classification, source, current revision, revision list
    r0001.md ...    immutable revision bytes, created exclusively (never overwritten)

``document.json`` is the commit point: a revision file written without a following metadata replace is an
orphan, is ignored by readers, and is discarded by the next successful write. SQLite is not touched.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from .models import Project
from .workspace import (READY, WorkspaceConflict, _atomic_replace, _chain_is_unsafe, _ensure_directory, _fsync_directory,
                        _temp_file, inspect_project, project_directory)

FORMAT = "rosc.architect-document.v1"
MAX_DOCUMENT_BYTES = 262_144
MAX_METADATA_BYTES = 262_144
MAX_REVISIONS = 1000
MAX_DOCUMENTS = 500
MEDIA_TYPES = {"text/markdown": ".md", "text/plain": ".txt"}

# Classification. Only ARCHITECT_OWNED is writable by this store. The other two are reserved vocabulary for a
# later, separately approved import; this slice never creates them and never promotes anything to them.
ARCHITECT_OWNED = "architect_owned"
EXTERNAL_CANONICAL = "external_canonical"
GOVERNANCE_REFERENCE = "governance_reference_snapshot"
CLASSIFICATIONS = (ARCHITECT_OWNED, EXTERNAL_CANONICAL, GOVERNANCE_REFERENCE)
SOURCE_LOCAL = "local_authored"

_DOC_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}", re.ASCII)
_SHA = re.compile(r"[0-9a-f]{64}", re.ASCII)
_REVISION_FILE = re.compile(r"r([0-9]{4,})\.(?:md|txt)", re.ASCII)


class StoreError(Exception):
    """Base class; every store failure is fail-closed and leaves previous valid bytes untouched."""


class InvalidDocument(StoreError):
    """Name, type, size or encoding is outside the contract."""


class DocumentNotFound(StoreError):
    pass


class DocumentExists(StoreError):
    pass


class RevisionConflict(StoreError):
    def __init__(self, message: str, current_revision: int | None) -> None:
        super().__init__(message)
        self.current_revision = current_revision


class UnsafeStore(StoreError):
    """Symlink, wrong file type, foreign identity, tampered bytes or unreadable metadata."""


class AuthorizationDenied(StoreError):
    pass


@dataclass(frozen=True)
class ActorContext:
    """Slot for the trusted actor produced by the future authorization boundary.

    It must never be built from request input or a caller-supplied agent id. This slice does not authenticate;
    ``actor_id`` is an opaque label recorded for provenance only.
    """

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
class Document:
    info: DocumentInfo
    content: bytes
    revision: int

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")


def normalize_doc_id(value: str) -> str:
    """Trim and lower-case, then require a plain slug. Separators, dots-only, absolute or traversal names are rejected."""
    if not isinstance(value, str):
        raise InvalidDocument("Document id must be text")
    doc_id = value.strip().lower()
    if not _DOC_ID.fullmatch(doc_id) or doc_id.strip(".") == "" or doc_id.endswith("."):
        raise InvalidDocument("Document id must be 1-64 characters of a-z, 0-9, '.', '_' or '-', starting with a letter or digit")
    return doc_id


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _project_label(project_id: int) -> str:
    return f"P{project_id:04d}"


class ArchitectDocumentStore:
    """Filesystem layer. Callers must already know the Project exists; the service below checks that."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root

    # -- paths -----------------------------------------------------------------------------------------------
    def _documents_root(self, project_id: int) -> Path:
        return project_directory(self.workspace_root, project_id) / "architect" / "documents"

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

    # -- low level reads ---------------------------------------------------------------------------------------
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

    def _load_meta(self, project_id: int, directory: Path, doc_id: str) -> dict:
        """Validate document.json against the directory it lives in. Anything unexpected is UnsafeStore."""
        if directory.is_symlink() or not directory.is_dir():
            raise UnsafeStore("Architect document directory is a symlink or not a directory")
        try:
            raw = self._read_regular(directory / "document.json", MAX_METADATA_BYTES)
        except FileNotFoundError:
            raise UnsafeStore("Architect document has no metadata") from None
        try:
            meta = json.loads(raw.decode("utf-8"))
            revisions = meta["revisions"]
            assert meta["format"] == FORMAT and meta["project_id"] == _project_label(project_id) and meta["doc_id"] == doc_id
            assert meta["classification"] == ARCHITECT_OWNED and meta["media_type"] in MEDIA_TYPES
            assert isinstance(meta["source"], dict) and isinstance(revisions, list) and 1 <= len(revisions) <= MAX_REVISIONS
            assert meta["revision"] == len(revisions) and isinstance(meta["created_at"], str) and isinstance(meta["updated_at"], str)
            for number, entry in enumerate(revisions, start=1):
                assert entry["revision"] == number and _SHA.fullmatch(entry["sha256"]) and isinstance(entry["size"], int)
                assert entry["file"] == f"r{number:04d}{MEDIA_TYPES[meta['media_type']]}"
                assert isinstance(entry["created_at"], str) and isinstance(entry["actor_id"], str)
        except (UnicodeDecodeError, ValueError, KeyError, TypeError, AssertionError) as exc:
            raise UnsafeStore("Architect document metadata is invalid, foreign or from another Project") from exc
        return meta

    @staticmethod
    def _info(meta: dict) -> DocumentInfo:
        revisions = tuple(RevisionInfo(**{k: e[k] for k in ("revision", "sha256", "size", "created_at", "actor_id", "file")}) for e in meta["revisions"])
        head = revisions[-1]
        return DocumentInfo(project_id=meta["project_id"], doc_id=meta["doc_id"], classification=meta["classification"],
                            source=dict(meta["source"]), media_type=meta["media_type"], revision=head.revision, sha256=head.sha256,
                            size=head.size, created_at=meta["created_at"], updated_at=meta["updated_at"], revisions=revisions)

    def _read_revision(self, directory: Path, entry: RevisionInfo) -> bytes:
        try:
            data = self._read_regular(directory / entry.file, MAX_DOCUMENT_BYTES)
        except FileNotFoundError:
            raise UnsafeStore("Architect document revision file is missing") from None
        if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
            raise UnsafeStore("Architect document revision does not match its recorded digest")
        return data

    # -- public operations ---------------------------------------------------------------------------------------
    def list(self, project_id: int) -> list[DocumentInfo]:
        root = self._existing_root(project_id)
        if root is None:
            return []
        found = []
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if child.name.startswith("."):
                continue  # lock file and temp files
            if not _DOC_ID.fullmatch(child.name):
                raise UnsafeStore("Unexpected entry in Architect document root")
            found.append(self._info(self._load_meta(project_id, child, child.name)))
        return found

    def read(self, project_id: int, doc_id: str, revision: int | None = None) -> Document:
        doc_id = normalize_doc_id(doc_id)
        root = self._existing_root(project_id)
        if root is None or not os.path.lexists(root / doc_id):
            raise DocumentNotFound(f"Document {doc_id} does not exist")
        directory = root / doc_id
        info = self._info(self._load_meta(project_id, directory, doc_id))
        number = info.revision if revision is None else revision
        if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= info.revision:
            raise DocumentNotFound(f"Document {doc_id} has no revision {revision}")
        return Document(info, self._read_revision(directory, info.revisions[number - 1]), number)

    def write(self, project_id: int, doc_id: str, content: str | bytes, *, actor_id: str, expected_revision: int | None = None,
              media_type: str = "text/markdown") -> DocumentInfo:
        """Create (expected_revision=None; refuses if present) or revise (expected_revision must equal the current head)."""
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
        if expected_revision is not None and (isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1):
            raise InvalidDocument("expected_revision must be a positive integer")

        root = self._prepare_root(project_id)
        lock_fd = os.open(root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            return self._write_locked(project_id, root, doc_id, data, actor_id, expected_revision, media_type)
        finally:
            os.close(lock_fd)

    def _write_locked(self, project_id: int, root: Path, doc_id: str, data: bytes, actor_id: str, expected: int | None, media_type: str) -> DocumentInfo:
        directory = root / doc_id
        stamp = _now()
        digest = hashlib.sha256(data).hexdigest()
        if not os.path.lexists(directory):
            if expected is not None:
                raise RevisionConflict(f"Document {doc_id} does not exist", None)
            if sum(1 for c in root.iterdir() if not c.name.startswith(".")) >= MAX_DOCUMENTS:
                raise InvalidDocument("Document count limit reached")
            os.mkdir(directory, 0o700)
            _fsync_directory(root)
            previous: dict | None = None
            number = 1
            meta = {"format": FORMAT, "project_id": _project_label(project_id), "doc_id": doc_id, "classification": ARCHITECT_OWNED,
                    "source": {"kind": SOURCE_LOCAL, "external_id": None, "external_revision": None, "verified_at": None},
                    "media_type": media_type, "created_at": stamp, "revisions": []}
        else:
            previous = self._load_meta(project_id, directory, doc_id)
            current = previous["revision"]
            self._read_revision(directory, self._info(previous).revisions[-1])  # the head must still be what was committed
            if expected is None:
                raise DocumentExists(f"Document {doc_id} already exists at revision {current}")
            if expected != current:
                raise RevisionConflict(f"Document {doc_id} is at revision {current}, not {expected}", current)
            if previous["media_type"] != media_type:
                raise InvalidDocument("Media type cannot change between revisions")
            if len(previous["revisions"]) >= MAX_REVISIONS:
                raise InvalidDocument("Revision limit reached")
            number = current + 1
            meta = dict(previous)
            meta["revisions"] = list(previous["revisions"])
        file_name = f"r{number:04d}{MEDIA_TYPES[media_type]}"
        target = directory / file_name
        self._discard_orphans(directory, number)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        meta["revisions"].append({"revision": number, "sha256": digest, "size": len(data), "created_at": stamp,
                                  "actor_id": str(actor_id), "file": file_name})
        meta["revision"] = number
        meta["updated_at"] = stamp
        temporary = _temp_file(directory, (json.dumps(meta, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        try:
            _atomic_replace(temporary, directory / "document.json")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return self._info(meta)

    @staticmethod
    def _discard_orphans(directory: Path, next_number: int) -> None:
        """Remove revision files the metadata never committed (crash or failed write); never touch committed ones."""
        for child in directory.iterdir():
            match = _REVISION_FILE.fullmatch(child.name)
            if child.name.startswith(".rosc-") and child.name.endswith(".tmp"):
                child.unlink()
            elif match and int(match.group(1)) >= next_number:
                if child.is_symlink() or not child.is_file():
                    raise UnsafeStore("Unexpected entry in Architect document directory")
                child.unlink()


class ArchitectDocumentService:
    """Core entrypoint: Project existence/readiness, then policy, then the store. No HTTP surface."""

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

    def list_documents(self, actor: ActorContext, project_id: int) -> list[DocumentInfo]:
        self._gate(actor, "list", project_id, None)
        return self._store.list(project_id)

    def read_document(self, actor: ActorContext, project_id: int, doc_id: str, revision: int | None = None) -> Document:
        self._gate(actor, "read", project_id, doc_id)
        return self._store.read(project_id, doc_id, revision)

    def write_document(self, actor: ActorContext, project_id: int, doc_id: str, content: str | bytes, *,
                       expected_revision: int | None = None, media_type: str = "text/markdown") -> DocumentInfo:
        self._gate(actor, "write", project_id, doc_id)
        return self._store.write(project_id, doc_id, content, actor_id=actor.actor_id, expected_revision=expected_revision, media_type=media_type)
