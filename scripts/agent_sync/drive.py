"""Optional Google Drive transport for agent prompt/report sync.

The smallest clean Drive v3 transport: list a folder, download a file, fetch one
file's metadata, and create a file (with optional ``appProperties`` for
reconciliation). It is intentionally decoupled from any other mechanism.

Scope decision: full ``drive``. The mechanism must read prompt drafts a human
created in a human-created folder *and* create report archive files in a
configured folder -- both are files/folders this OAuth app did not itself create,
which ``drive.file`` cannot reach, and ``drive.readonly`` cannot write. ``drive``
is therefore the least single scope that works; it requires one interactive
re-authorization (``python3 -m scripts.agent_sync.cli auth``), documented in
``docs/agent_reporting.md``.

The Google client libraries are an *optional* dependency imported lazily inside
the functions that need them, so every local artifact command and the offline
test suite run with the standard library alone.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

SCOPES = ["https://www.googleapis.com/auth/drive"]

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
FOLDER_MIME = "application/vnd.google-apps.folder"
MARKDOWN_MIME = "text/markdown"
PLAIN_TEXT_MIME = "text/plain"
OCTET_STREAM_MIME = "application/octet-stream"
MARKDOWN_COMPATIBLE_MIMES = frozenset(
    {MARKDOWN_MIME, PLAIN_TEXT_MIME, OCTET_STREAM_MIME}
)

__all__ = [
    "SCOPES",
    "GOOGLE_DOC_MIME",
    "FOLDER_MIME",
    "MARKDOWN_MIME",
    "MARKDOWN_COMPATIBLE_MIMES",
    "DriveFile",
    "DriveTransport",
    "GoogleDriveTransport",
    "AuthRequiredError",
    "load_credentials",
    "run_interactive_authorization",
    "write_token",
    "build_drive_service",
    "open_transport",
]


class AuthRequiredError(RuntimeError):
    """Raised when non-interactive credential loading cannot produce valid
    credentials and interactive re-authentication is required (or the optional
    Google client libraries are not installed)."""


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    modified_time: str | None = None
    parents: tuple[str, ...] = ()
    app_properties: dict[str, str] = field(default_factory=dict)

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME

    @property
    def is_google_doc(self) -> bool:
        return self.mime_type == GOOGLE_DOC_MIME


class DriveTransport(Protocol):
    """The narrow surface ``sync.py`` needs. Implemented for real by
    :class:`GoogleDriveTransport` and by fakes in tests."""

    def list_children(self, folder_id: str) -> list[DriveFile]: ...

    def download(self, file_id: str) -> bytes: ...

    def get_file(self, file_id: str) -> DriveFile | None: ...

    def create_file(
        self,
        *,
        name: str,
        content: bytes,
        folder_id: str,
        mime_type: str = MARKDOWN_MIME,
        app_properties: dict[str, str] | None = None,
    ) -> DriveFile: ...


def _google_auth_modules():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    return Request, Credentials


def load_credentials(credentials_path: Path, token_path: Path, *, scopes: list[str] | None = None):
    """Load credentials non-interactively.

    Never starts an interactive browser/device authorization flow. Attempts a
    normal refresh-token renewal when the stored token is expired. Raises
    :class:`AuthRequiredError` (never a bare crash) when interactive
    re-authentication or installing the optional dependencies would be required.
    """

    try:
        Request, Credentials = _google_auth_modules()
    except ImportError as exc:
        raise AuthRequiredError(
            "Google API client libraries are not installed. Create the documented "
            "per-project virtual environment and install the optional Drive extra "
            "before using prompt-pull/reports-push."
        ) from exc

    if not token_path.exists():
        raise AuthRequiredError(
            f"No stored credentials at {token_path}; interactive sign-in required."
        )

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    except Exception as exc:  # noqa: BLE001 - any malformed token is equally non-fatal here
        raise AuthRequiredError(
            f"Stored credentials at {token_path} could not be read "
            f"({exc.__class__.__name__}); token content was not displayed."
        ) from exc

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:  # noqa: BLE001 - refresh failures all mean "needs interactive renewal"
            raise AuthRequiredError(
                "Stored refresh token was rejected while renewing credentials "
                f"({exc.__class__.__name__}); token content was not displayed."
            ) from exc
        write_token(creds, token_path)
        return creds

    raise AuthRequiredError(
        "Stored credentials are invalid and cannot be refreshed non-interactively; "
        "interactive re-authentication is required."
    )


def run_interactive_authorization(
    credentials_path: Path, token_path: Path, scopes: list[str] | None = None
):
    """Run the installed-app OAuth flow interactively (opens a browser, completes
    the localhost callback) and return fresh, valid credentials.

    Does not touch ``token_path`` itself; callers write the token only after this
    returns successfully (see :func:`write_token`), so a cancelled or failed flow
    never disturbs an existing token. Explicitly requests offline access and
    forces the consent screen, because Google only issues a refresh token on the
    first authorization of a client unless prompted.
    """

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise AuthRequiredError(
            "Google OAuth libraries are not installed. Create the documented "
            "per-project virtual environment and install the optional Drive extra "
            "before running interactive auth."
        ) from exc

    if not credentials_path.exists():
        raise AuthRequiredError(
            f"OAuth client secrets file not found: {credentials_path}. Download it "
            "from Google Cloud Console as credentials.json."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(credentials_path), scopes or SCOPES
    )
    return flow.run_local_server(port=0, access_type="offline", prompt="consent")


def write_token(creds, token_path: Path) -> None:
    """Atomically replace ``token_path`` with ``creds``.

    Writes to a sibling temporary file first and only then renames it over the
    real path, so a crash mid-write can never leave a partially-written token,
    and the previous token is only replaced once the new one is fully written.
    """

    parent = token_path.parent
    if parent.exists() and not parent.is_dir():
        raise OSError(f"Token parent path is not a directory: {parent}")
    if not parent.exists():
        # A newly-created credentials directory should not be traversable by
        # other users. Existing project directories are intentionally left
        # alone; the token file itself remains owner-only.
        parent.mkdir(parents=True, mode=0o700)

    if os.name == "posix" and token_path.exists():
        # Best effort for a legacy token. Failure aborts before touching its
        # bytes, rather than replacing a file whose permissions we cannot make
        # owner-only.
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass

    fd = -1
    tmp_path: Path | None = None
    try:
        fd, raw_tmp_path = tempfile.mkstemp(
            dir=parent, prefix=f".{token_path.name}.", suffix=".tmp"
        )
        tmp_path = Path(raw_tmp_path)
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        payload = creds.to_json().encode("utf-8")
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, token_path)
        tmp_path = None
        if os.name == "posix":
            # The replaced tempfile was already 0600; this is a best-effort
            # assertion for unusual filesystems rather than a second failure
            # point after the previous token has been replaced.
            try:
                os.chmod(token_path, 0o600)
            except OSError:
                pass
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def build_drive_service(creds):
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def open_transport(config) -> "GoogleDriveTransport":
    """Load non-interactive credentials and build a real Drive transport.

    Raises :class:`AuthRequiredError` (never a bare crash) when interactive
    re-authentication is required -- the caller converts that to a clear,
    retryable message without touching any local artifact.
    """

    creds = load_credentials(
        config.credentials_path, config.token_path, scopes=SCOPES
    )
    return GoogleDriveTransport(build_drive_service(creds))


def _file_from_api(data: dict) -> DriveFile:
    return DriveFile(
        id=data["id"],
        name=data.get("name", ""),
        mime_type=data.get("mimeType", ""),
        modified_time=data.get("modifiedTime"),
        parents=tuple(data.get("parents") or ()),
        app_properties=dict(data.get("appProperties") or {}),
    )


_FILE_FIELDS = "id,name,mimeType,modifiedTime,parents,appProperties"


def _escape_query_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveTransport:
    """Real Drive v3 implementation of :class:`DriveTransport`."""

    def __init__(self, service):
        self._service = service

    def list_children(self, folder_id: str) -> list[DriveFile]:
        query = f"'{_escape_query_literal(folder_id)}' in parents and trashed = false"
        files: list[DriveFile] = []
        page_token: str | None = None
        while True:
            response = (
                self._service.files()
                .list(
                    q=query,
                    pageSize=1000,
                    pageToken=page_token,
                    fields=f"nextPageToken, files({_FILE_FIELDS})",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            files.extend(_file_from_api(item) for item in response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return files

    def get_file(self, file_id: str) -> DriveFile | None:
        from googleapiclient.errors import HttpError

        try:
            data = (
                self._service.files()
                .get(fileId=file_id, fields=_FILE_FIELDS, supportsAllDrives=True)
                .execute()
            )
        except HttpError as exc:
            if getattr(exc.resp, "status", None) == 404:
                return None
            raise
        return _file_from_api(data)

    def download(self, file_id: str) -> bytes:
        from .errors import DriveFormatError

        meta = self.get_file(file_id)
        if meta is not None and meta.is_google_doc:
            raise DriveFormatError(
                f"Drive draft {file_id} is a Google Doc, but the canonical agent "
                "prompt draft format is a Markdown file (.md). Save the prompt as a "
                ".md file in the drafts folder instead."
            )
        data = (
            self._service.files()
            .get_media(fileId=file_id, supportsAllDrives=True)
            .execute()
        )
        if isinstance(data, bytes):
            return data
        return bytes(data)

    def create_file(
        self,
        *,
        name: str,
        content: bytes,
        folder_id: str,
        mime_type: str = MARKDOWN_MIME,
        app_properties: dict[str, str] | None = None,
    ) -> DriveFile:
        from googleapiclient.http import MediaInMemoryUpload

        body: dict = {"name": name, "parents": [folder_id]}
        if app_properties:
            body["appProperties"] = app_properties
        media = MediaInMemoryUpload(content, mimetype=mime_type, resumable=False)
        data = (
            self._service.files()
            .create(
                body=body,
                media_body=media,
                fields=_FILE_FIELDS,
                supportsAllDrives=True,
            )
            .execute()
        )
        return _file_from_api(data)

