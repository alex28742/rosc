"""Trusted Architect rules loader (P021): verified, deterministic, immutable ruleset snapshot.

Trust model: the rule bytes are trusted only because the Owner accepted them and they are installed in the reviewed Git
checkout under ``global/``.  This module never trusts a path, name or metadata that an Agent, a Project, the current
directory or a document supplies.  The only inputs are the checkout library root (``rosc.config.global_library_dir()``,
injected by the composition root) and the static allowlist below.  A file existing on disk grants nothing; a rule's prose
never substitutes for Core authorization.

``ArchitectRulesLoader.load()`` either returns a fully verified :class:`RulesSnapshot` or raises :class:`RulesError`
with a short non-secret code.  There is no permissive fallback: a missing, malformed, tampered, symlinked, oversized or
re-written file refuses the whole ruleset.  No network, no code execution, no DB.
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

import yaml

RULESET_PATH = "agents/architect/ruleset.yaml"
SCHEMA_PATH = "agents/architect/documentation-standard.yaml"
PRINCIPLE_PATH = "principles/context-economy.md"
RULES_DIR = "agents/architect/rules/"
# Static allowlist.  The manifest may only describe exactly these, in exactly this order.
ACTIVE_RULES = (("role", RULES_DIR + "role.md"), ("onboarding", RULES_DIR + "onboarding.md"),
                ("context-maintenance", RULES_DIR + "context-maintenance.md"),
                ("pipeline-management", RULES_DIR + "pipeline-management.md"))
REFERENCE_RULES = tuple((name, RULES_DIR + name + ".md") for name in (
    "autonomy", "research", "review", "communication", "run-checkpoints", "worker-task-authoring", "task-run-git"))
# Every path a pinned session must only ever see as the exact verified bytes (also used by the read Tool to decide
# which global paths are policy-governed even when no pin exists).
GOVERNED_PATHS = frozenset({RULESET_PATH, SCHEMA_PATH, PRINCIPLE_PATH,
                            *(path for _, path in ACTIVE_RULES), *(path for _, path in REFERENCE_RULES)})
MANIFEST_FORMAT = "rosc.architect.ruleset.v1"
SCHEMA_FORMAT = "rosc.architect.documentation-standard.v1"
RULESET_ID = "rosc.architect.global"
REQUIRED_CORE_RESOURCES = ("system/identity", "system/onboarding", "system/events", "pipeline/main")

MAX_MANIFEST_BYTES = 32 * 1024
MAX_RULE_BYTES = 64 * 1024
MAX_BOOTSTRAP_CHARS = 6_000      # what one prompt may carry of the rules (identity + role + R-14 quote + index)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}")


class RulesError(Exception):
    """Refusal with a stable non-secret ``code`` (also used as the persisted Architect session error code)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RuleFile:
    id: str
    path: str            # relative to ``global/``
    kind: str            # active | schema | principle | reference
    sha256: str
    size: int
    text: str


@dataclass(frozen=True)
class RulesSnapshot:
    """Immutable, session-pinned view of the verified ruleset."""
    ruleset_id: str
    version: str
    digest: str
    files: tuple[RuleFile, ...]
    bootstrap: str       # bounded effective text proven to reach the model (identity + role + R-14 quote + index)
    manifest_sha256: str  # sha256 of the manifest bytes verified at activation (not part of ``digest``)

    def pinned_sha256(self, path: str) -> str | None:
        """The sha256 verified at activation for a governed ``global/``-relative path, else ``None``."""
        if path == RULESET_PATH:
            return self.manifest_sha256
        return next((f.sha256 for f in self.files if f.path == path), None)

    def identity(self) -> dict:
        return {"ruleset_id": self.ruleset_id, "version": self.version, "digest": self.digest}

    def inventory(self) -> list[dict]:
        return [{"id": f.id, "path": f.path, "kind": f.kind, "sha256": f.sha256, "bytes": f.size} for f in self.files]

    def prompt_block(self, tools_on: bool) -> str:
        """The block placed in every Architect turn.  ``tools_on`` only changes how on-demand rules are described."""
        if tools_on:
            index = ("Other active rules are NOT pasted here; read them with rosc_global_read when the work needs them:\n")
        else:
            index = ("This session has no tools, so the other rules below cannot be read here; do not claim to have read "
                     "them:\n")
        lines = [f"- {f.path} (sha256 {f.sha256[:12]})" for f in self.files if f.kind in ("active", "principle")
                 and f.id != "role"]
        return (f"=== ROSC GLOBAL ARCHITECT RULES ruleset={self.ruleset_id} version={self.version} "
                f"digest={self.digest} ===\n{self.bootstrap}\n{index}" + "\n".join(lines)
                + "\n=== END ROSC GLOBAL ARCHITECT RULES ===\n")


# -- strict YAML ---------------------------------------------------------------------------------------------------------
class _StrictLoader(yaml.SafeLoader):
    """``safe_load`` that refuses duplicate mapping keys instead of silently keeping the last one."""

    def construct_mapping(self, node, deep=False):  # type: ignore[override]
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            try:
                if key in seen:
                    raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
                seen.add(key)
            except TypeError:
                raise yaml.constructor.ConstructorError(None, None, "unhashable key", key_node.start_mark) from None
        return super().construct_mapping(node, deep)


def _parse_yaml(data: bytes, what: str) -> object:
    try:
        return yaml.load(data.decode("utf-8"), Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError):
        raise RulesError("rules_malformed", f"{what} is not valid UTF-8 YAML (or has duplicate keys)") from None


# -- no-symlink bounded reads --------------------------------------------------------------------------------------------
def _open_root(root: Path) -> int:
    try:
        return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise RulesError("rules_library_unsafe", "The global library root is a symlink or not a directory") from None
        raise RulesError("rules_library_unavailable", "The global library root is not available") from None


def _read_regular(root_fd: int, rel: str, limit: int) -> bytes:
    """Read ``rel`` below ``root_fd`` without following any symlink; regular, single-link, bounded, stable."""
    parts = rel.split("/")
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
            except OSError as exc:
                code = "rules_path_unsafe" if exc.errno in (errno.ELOOP, errno.ENOTDIR) else "rules_file_missing"
                raise RulesError(code, "A ruleset directory is missing or unsafe") from None
            os.close(current)
            current = nxt
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=current)
        except OSError as exc:
            code = "rules_path_unsafe" if exc.errno in (errno.ELOOP, errno.ENXIO) else "rules_file_missing"
            raise RulesError(code, "A ruleset file is missing or unsafe") from None
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise RulesError("rules_path_unsafe", "A ruleset file is not a plain single-link regular file")
            if before.st_size > limit:
                raise RulesError("rules_file_too_large", "A ruleset file is larger than the allowed size")
            data = b""
            while len(data) <= limit:
                chunk = os.read(fd, min(65536, limit + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
            after = os.fstat(fd)
            if len(data) > limit:
                raise RulesError("rules_file_too_large", "A ruleset file is larger than the allowed size")
            if (after.st_size, after.st_mtime_ns, after.st_ino) != (before.st_size, before.st_mtime_ns, before.st_ino) \
                    or len(data) != after.st_size:
                raise RulesError("rules_changed_during_read", "A ruleset file changed while it was being read")
            return data
        finally:
            os.close(fd)
    finally:
        os.close(current)


def _decode(data: bytes, what: str) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise RulesError("rules_malformed", f"{what} is not valid UTF-8") from None
    if "\x00" in text or "\r" in text:
        raise RulesError("rules_malformed", f"{what} contains NUL or CR characters")
    return text


# -- manifest validation -------------------------------------------------------------------------------------------------
def _entry(raw: object, expected_id: str, expected_path: str, section: str) -> dict:
    if not isinstance(raw, dict) or set(raw) != {"id", "path", "source_sha256", "sha256", "bytes"}:
        raise RulesError("rules_manifest_invalid", f"{section} entry has unexpected fields")
    if raw["id"] != expected_id or raw["path"] != expected_path:
        raise RulesError("rules_manifest_invalid", f"{section} entry is not the approved {expected_id}")
    if not (isinstance(raw["sha256"], str) and _SHA256.fullmatch(raw["sha256"])
            and isinstance(raw["source_sha256"], str) and _SHA256.fullmatch(raw["source_sha256"])
            and isinstance(raw["bytes"], int) and not isinstance(raw["bytes"], bool) and 0 < raw["bytes"] <= MAX_RULE_BYTES):
        raise RulesError("rules_manifest_invalid", f"{section} entry has an invalid digest or size")
    return raw


def _validate_manifest(doc: object) -> tuple[str, str, list[tuple[str, dict]]]:
    top = {"format", "ruleset_id", "version", "status", "provenance", "requires_explicit_activation", "schema_reference",
           "active_bootstrap", "foundation_principle", "reference_on_demand"}
    if not isinstance(doc, dict) or set(doc) != top:
        raise RulesError("rules_manifest_invalid", "The ruleset manifest has unexpected top-level fields")
    if doc["format"] != MANIFEST_FORMAT or doc["ruleset_id"] != RULESET_ID or doc["status"] != "approved_definition":
        raise RulesError("rules_manifest_invalid", "The ruleset manifest identity or status is not the approved one")
    version = doc["version"]
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise RulesError("rules_manifest_invalid", "The ruleset version is not a plain x.y.z string")
    if doc["requires_explicit_activation"] is not True:
        raise RulesError("rules_manifest_invalid", "The ruleset must require explicit activation")
    prov = doc["provenance"]
    if not isinstance(prov, dict) or prov.get("accepted_by") != "Owner":
        raise RulesError("rules_manifest_invalid", "The ruleset has no Owner acceptance provenance")
    active, reference = doc["active_bootstrap"], doc["reference_on_demand"]
    if not isinstance(active, list) or len(active) != len(ACTIVE_RULES) \
            or not isinstance(reference, list) or len(reference) != len(REFERENCE_RULES):
        raise RulesError("rules_manifest_invalid", "The ruleset file lists differ from the approved sets")
    entries = [("schema", _entry(doc["schema_reference"], "documentation-standard", SCHEMA_PATH,
                                 "schema_reference") | {"kind": "schema"})]
    entries += [("active", _entry(raw, name, path, "active_bootstrap") | {"kind": "active"})
                for raw, (name, path) in zip(active, ACTIVE_RULES)]
    entries.append(("principle", _entry(doc["foundation_principle"], "context-economy", PRINCIPLE_PATH,
                                        "foundation_principle") | {"kind": "principle"}))
    entries += [("reference", _entry(raw, name, path, "reference_on_demand") | {"kind": "reference"})
                for raw, (name, path) in zip(reference, REFERENCE_RULES)]
    return RULESET_ID, version, entries


def _validate_schema_doc(doc: object, version: str) -> None:
    if not isinstance(doc, dict) or doc.get("format") != SCHEMA_FORMAT or doc.get("version") != version \
            or doc.get("status") != "approved_definition" or doc.get("runtime_tool_grants") != "none":
        raise RulesError("rules_schema_invalid", "The documentation standard is not the approved matching version")
    required = doc.get("required_resources")
    core = required.get("core") if isinstance(required, dict) else None
    docs = required.get("architect_documents") if isinstance(required, dict) else None
    pipeline = required.get("pipeline") if isinstance(required, dict) else None
    if core != list(REQUIRED_CORE_RESOURCES) or not isinstance(docs, list) \
            or not any(isinstance(d, dict) and d.get("logical_path") == "handoff.md" for d in docs) \
            or not isinstance(pipeline, dict) or pipeline.get("markdown_export") != "generated_read_only":
        raise RulesError("rules_schema_invalid", "The documentation standard lacks the required logical resources")


def _bootstrap_text(files: dict[str, RuleFile]) -> str:
    quote = [line for line in files["context-economy"].text.splitlines() if line.startswith("> ")]
    if len(quote) < 2:
        raise RulesError("rules_malformed", "The R-14 principle statement is missing")
    text = (files["role"].text.rstrip("\n") + "\n\nFoundation principle R-14 (global/" + PRINCIPLE_PATH + "):\n"
            + "\n".join(quote) + "\n")
    if len(text) > MAX_BOOTSTRAP_CHARS:
        raise RulesError("rules_bootstrap_too_large", "The effective rule text exceeds its prompt budget")
    return text


class ArchitectRulesLoader:
    """Stateless verifier.  The service pins the returned snapshot per session; this class never caches."""

    def __init__(self, library_root: Path | None) -> None:
        self._root = library_root

    @property
    def configured(self) -> bool:
        return self._root is not None

    def load(self) -> RulesSnapshot:
        if self._root is None:
            raise RulesError("rules_library_unavailable", "No ROSC source checkout: the global library cannot be located")
        root_fd = _open_root(Path(self._root))
        try:
            manifest_bytes = _read_regular(root_fd, RULESET_PATH, MAX_MANIFEST_BYTES)
            _decode(manifest_bytes, "The ruleset manifest")
            ruleset_id, version, entries = _validate_manifest(_parse_yaml(manifest_bytes, "The ruleset manifest"))
            files: list[RuleFile] = []
            for kind, entry in entries:
                data = _read_regular(root_fd, entry["path"], MAX_RULE_BYTES)
                text = _decode(data, entry["path"])
                digest = hashlib.sha256(data).hexdigest()
                if digest != entry["sha256"] or len(data) != entry["bytes"]:
                    raise RulesError("rules_hash_mismatch", "A ruleset file does not match its approved digest")
                files.append(RuleFile(entry["id"], entry["path"], entry["kind"], digest, len(data), text))
        finally:
            os.close(root_fd)
        by_id = {f.id: f for f in files}
        _validate_schema_doc(_parse_yaml(by_id["documentation-standard"].text.encode(), SCHEMA_PATH), version)
        composed = hashlib.sha256()
        composed.update(f"{ruleset_id}\n{version}\n".encode())
        for f in files:
            composed.update(f"{f.kind}\t{f.id}\t{f.path}\t{f.sha256}\n".encode())
        return RulesSnapshot(ruleset_id, version, composed.hexdigest(), tuple(files), _bootstrap_text(by_id),
                             hashlib.sha256(manifest_bytes).hexdigest())
