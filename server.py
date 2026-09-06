#!/usr/bin/env python3
"""Google Workspace MCP server: Sheets, Drive, Docs, Forms, Gmail, Calendar,
People, Slides, Tasks, Chat, Meet.

Auth: uses the token created by the setup flow (`setup/setup.py --auth-url`, then `--auth-code`):
  token  -> <GOOGLE_WORKSPACE_HOME>/google_token.json
  client -> <GOOGLE_WORKSPACE_HOME>/google_client_secret.json
Auto-refreshes via google-auth-oauthlib. State home resolution:
`GOOGLE_WORKSPACE_HOME` env, else legacy `HERMES_HOME`, else `<server.py dir>/state`.

Return-payload policy (2026-08-11): read tools return the FULL API resource
(types, required flags, validation rules, options, attendees, labels, etc.)
instead of hand-picked field subsets. Where a raw resource is too large to be
useful as-is (docs body, slides deck, gmail body), a structured extraction is
returned with all identifying metadata attached.

Write safety policy (2026-08-11): NO write tool touches Google directly.
Every write tool STAGES the operation: it takes a snapshot, runs deterministic
checks, and returns an operation_id + preview WITHOUT applying anything. The
agent reviews the preview, then applies via google_write_commit(operation_id)
(single-use; revalidates against live state first) or discards via
google_write_cancel(operation_id). Read tools are never gated.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import random
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httplib2
from filelock import FileLock
from mcp.server.fastmcp import FastMCP
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.discovery_cache.base import Cache
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from mcp.types import ToolAnnotations

mcp = FastMCP(
    "google-workspace",
    instructions=(
        "Google Workspace (staged writes). Reads run immediately; every mutating tool "
        "only STAGES and returns operation_id + preview - nothing external changes until "
        "google_write_commit. Flow: read-first (get/search/metadata) -> stage -> "
        "google_write_list_staged to review -> google_write_commit or google_write_cancel "
        "(single-use, revalidates on commit)."
    ),
)


def _state_home() -> Path:
    for var in ("GOOGLE_WORKSPACE_HOME", "HERMES_HOME"):
        env = (os.environ.get(var) or "").strip()
        if env:
            return Path(env)
    return Path(__file__).resolve().parent / "state"


TOKEN_PATH = Path(os.environ.get("GOOGLE_TOKEN_PATH", _state_home() / "google_token.json"))
CLIENT_PATH = Path(os.environ.get("GOOGLE_CLIENT_SECRET_PATH", _state_home() / "google_client_secret.json"))
DOWNLOAD_DIR = Path(os.environ.get("GOOGLE_DOWNLOAD_DIR", _state_home() / "downloads" / "google"))
AUDIT_LOG = _state_home() / "logs" / "google-write-audit.jsonl"


class _MemoryDiscoveryCache(Cache):
    """In-memory discovery-doc cache (FileCache is unavailable with google-auth).

    v2 stdio is single-threaded: no lock. A daemon migration must guard this."""

    def __init__(self, max_entries: int = 16):
        self._docs: dict = {}
        self._max = max_entries

    def get(self, url):
        return self._docs.get(url)

    def set(self, url, content):
        if len(self._docs) >= self._max:
            self._docs.pop(next(iter(self._docs)))
        self._docs[url] = content


_DISCOVERY_CACHE = _MemoryDiscoveryCache()
_CREDS = None            # process-singleton Credentials (v2 stdio: single-threaded)
_SERVICES: dict = {}     # (api_name, version) -> built Resource
_MAX_GMAIL_FANOUT = 20
_READ_TIMEOUT, _MEDIA_TIMEOUT, _UPLOAD_TIMEOUT = 30, 60, 120


def _secure_write_json(path: Path, obj: dict) -> None:
    """Atomic JSON write with owner-only perms where supported."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _token_lock() -> FileLock:
    return FileLock(str(TOKEN_PATH) + ".lock", timeout=10)


def _get_creds() -> Credentials:
    global _CREDS
    if _CREDS is not None and _CREDS.valid:
        return _CREDS
    if not TOKEN_PATH.exists():
        raise RuntimeError(
            f"No token at {TOKEN_PATH}. Run the setup flow "
            "(setup.py --auth-url, then --auth-code) first."
        )
    with _token_lock():
        info = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
        creds = Credentials.from_authorized_user_info(info, scopes=info.get("scopes"))
        if creds.valid:
            _CREDS = creds
            return creds
        if not creds.expired or not creds.refresh_token:
            raise RuntimeError("Token invalid and not refreshable. Re-run setup.py.")
        if CLIENT_PATH.exists():
            # google-auth >= 2.55 makes client_id read-only; rebuild from merged info
            # instead of mutating attributes.
            client = json.loads(CLIENT_PATH.read_text(encoding="utf-8"))
            c = client.get("installed") or client.get("web") or {}
            merged = dict(info)
            merged["client_id"] = c.get("client_id") or info.get("client_id")
            merged["client_secret"] = c.get("client_secret") or info.get("client_secret")
            merged["token_uri"] = c.get("token_uri") or info.get("token_uri")
            creds = Credentials.from_authorized_user_info(merged, scopes=info.get("scopes"))
        creds.refresh(Request())
        _secure_write_json(TOKEN_PATH, json.loads(creds.to_json()))
        _CREDS = creds
        return creds


def _invalidate_auth() -> None:
    """Drop cached creds/services (after 401). Next call re-reads + refreshes."""
    global _CREDS
    _CREDS = None
    _SERVICES.clear()


def _svc(name: str, version: str):
    key = (name, version)
    if key not in _SERVICES:
        _SERVICES[key] = build(
            name, version, credentials=_get_creds(),
            cache=_DISCOVERY_CACHE, num_retries=0,
        )
    return _SERVICES[key]


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    if isinstance(exc, HttpError):
        status = exc.resp.status if exc.resp is not None else 0
        if status in (429, 500, 502, 503, 504):
            return True
        if status == 403:
            reason = ""
            try:
                reason = (exc.error_details or [{}])[0].get("reason", "")
            except Exception:
                pass
            if not reason:
                try:
                    reason = json.loads(exc.content.decode("utf-8", "ignore"))["error"].get("status", "")
                except Exception:
                    pass
            return reason in ("rateLimitExceeded", "userRateLimitExceeded",
                              "quotaExceeded", "backendError")
    return False


def _exec(req, kind: str = "read", timeout: int | None = None):
    """Execute a Google API request with timeouts + bounded retries.

    kind: read | revalidate | commit. Commits retry at most once and only
    pre-response rate limits; ambiguous timeouts surface instead of blind retry."""
    timeout = _READ_TIMEOUT if timeout is None else timeout
    attempts = 4 if kind == "read" else (2 if kind == "revalidate" else 2)
    tried_auth_refresh = False
    for attempt in range(attempts):
        try:
            return req.execute(http=httplib2.Http(timeout=timeout))
        except HttpError as e:
            status = e.resp.status if e.resp is not None else 0
            if status == 401 and not tried_auth_refresh:
                tried_auth_refresh = True
                _invalidate_auth()
                continue
            if kind == "commit" and status not in (429, 503):
                raise
            if not _retryable(e) or attempt >= attempts - 1:
                raise
            delay = min(1.0 * 2 ** attempt + random.uniform(0, 0.5), 20)
            time.sleep(delay)
        except Exception as e:
            if kind == "commit" and attempt >= 1:
                raise
            if not _retryable(e):
                raise
            if attempt >= attempts - 1:
                raise
            delay = min(1.0 * 2 ** attempt + random.uniform(0, 0.5), 20)
            time.sleep(delay)
    raise RuntimeError("unreachable: retry loop exhausted")


# ---------------------------------------------------------------- Staged writes

_STAGED: dict = {}  # operation_id -> operation record (in-memory, per session)
STAGED_TTL_SECONDS = 24 * 3600  # staged ops expire after 24h (never applied, just dropped)
STAGED_MAX_OPS = 20


def _purge_expired() -> int:
    """Drop expired staged ops (never applied, just forgotten). Returns count purged."""
    now = time.time()
    expired = [oid for oid, op in _STAGED.items()
               if now - op.get("created_ts", 0) > STAGED_TTL_SECONDS]
    for oid in expired:
        _STAGED.pop(oid, None)
    return len(expired)


_AUDIT_MAX_BYTES = 5 * 1024 * 1024


def _audit_rotate() -> None:
    try:
        if AUDIT_LOG.exists() and AUDIT_LOG.stat().st_size > _AUDIT_MAX_BYTES:
            for i in (3, 2, 1):
                src, dst = f"{AUDIT_LOG}.{i}.gz", f"{AUDIT_LOG}.{i + 1}.gz"
                if Path(src).exists():
                    Path(src).replace(dst)
            import gzip
            with open(AUDIT_LOG, "rb") as fh:
                data = fh.read()
            with gzip.open(f"{AUDIT_LOG}.1.gz", "wb") as fh:
                fh.write(data)
            AUDIT_LOG.write_text("", encoding="utf-8")
            try:
                os.chmod(AUDIT_LOG, 0o600)
            except Exception:
                pass
    except Exception:
        pass


def _audit_scrub_args(tool: str, args: dict) -> dict:
    """Keep identity + counts, drop bodies. Never logs values/text/attendees."""
    kept: dict = {}
    for k, v in (args or {}).items():
        if k.endswith("_id") or k.endswith("_ids") or k in (
                "range_", "range", "role", "status", "space_name",
                "tasklist_id", "calendar_id", "mimeType"):
            kept[k] = v
        elif k in ("title", "name", "subject", "summary") and isinstance(v, str):
            kept[k] = v[:120]
        elif isinstance(v, (list, tuple)):
            kept[k + "_count"] = len(v)
        elif isinstance(v, str):
            kept[k + "_chars"] = len(v)
        elif isinstance(v, (int, float, bool)) or v is None:
            kept[k] = v
        else:
            kept[k + "_type"] = type(v).__name__
    return kept


def _audit_scrub_result(result) -> dict:
    if not isinstance(result, dict):
        return {"result_type": type(result).__name__}
    kept: dict = {}
    for k in ("id", "documentId", "spreadsheetId", "presentationId",
              "name", "title", "status", "trashed", "htmlLink",
              "spreadsheetUrl", "url"):
        if k in result:
            v = result[k]
            kept[k] = v[:200] if isinstance(v, str) else v
    return kept


def _audit_event(event: str, op_id: str, tool: str,
                 args: dict | None = None, result=None, error: str | None = None) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        _audit_rotate()
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "operation_id": op_id,
            "tool": tool,
        }
        if args is not None:
            entry["args"] = _audit_scrub_args(tool, args)
        if result is not None:
            entry["result"] = _audit_scrub_result(result)
        if error is not None:
            entry["error"] = error[:300]
        with open(AUDIT_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                pass
    except Exception:
        pass  # audit must never break the flow


def _audit(op_id: str, tool: str, args: dict, result) -> None:
    _audit_event("commit", op_id, tool, args=args, result=result)


def _stage(tool: str, args: dict, apply_fn, checks: list, preview: dict, revalidate=None) -> dict:
    """Register a staged write operation. Nothing touches Google until commit."""
    if not callable(apply_fn):
        raise TypeError(
            f"_stage called without a callable apply_fn for {tool} (got {type(apply_fn).__name__}); "
            "arguments were probably passed in the wrong order"
        )
    op_id = secrets.token_hex(6)
    _purge_expired()
    if len(_STAGED) >= STAGED_MAX_OPS:
        raise RuntimeError(
            f"Too many staged operations ({STAGED_MAX_OPS} max). Commit or cancel "
            "existing ones first (google_write_list_staged)."
        )
    _STAGED[op_id] = {
        "tool": tool, "args": args, "apply": apply_fn,
        "revalidate": revalidate, "checks": checks, "preview": preview,
        "staged_at": datetime.now(timezone.utc).isoformat(),
        "created_ts": time.time(),
    }
    _audit_event("stage", op_id, tool, args=args)
    return {
        "staged": True,
        "operation_id": op_id,
        "tool": tool,
        "checks": checks,
        "preview": preview,
        "next_step": (
            "Nothing has been applied. Review the preview and checks, then call "
            "google_write_commit(operation_id) to apply, or "
            "google_write_cancel(operation_id) to discard."
        ),
    }


@mcp.tool(title='Commit Staged Write', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
def google_write_commit(operation_id: str) -> dict:
    """APPLY a staged Google Workspace write operation. Single-use: after commit the
    operation is gone. Revalidates against live state first (refuses if the resource
    changed or disappeared since staging). Logs every commit to the audit file.
    FAILS CLOSED in unattended cron runs (HERMES_CRON_SESSION set) unless
    HERMES_ALLOW_CRON_WRITES=1 is explicitly set."""
    if os.environ.get("HERMES_CRON_SESSION") and not os.environ.get("HERMES_ALLOW_CRON_WRITES"):
        raise RuntimeError(
            "Commit blocked: this is an unattended cron session (HERMES_CRON_SESSION set). "
            "Google writes fail closed in cron. Re-run interactively to apply staged "
            "operations, or set HERMES_ALLOW_CRON_WRITES=1 to explicitly allow."
        )
    _purge_expired()
    op = _STAGED.pop(operation_id, None)
    if op is None:
        raise RuntimeError(f"Unknown, cancelled, or already-applied operation: {operation_id}")
    if op.get("revalidate"):
        try:
            op["revalidate"]()
        except Exception as e:
            _audit_event("revalidate_fail", operation_id, op["tool"],
                         args=op.get("args"), error=str(e))
            raise
    try:
        result = op["apply"]()
    except Exception as e:
        _audit_event("commit_error", operation_id, op["tool"],
                     args=op.get("args"), error=str(e))
        raise
    _audit(operation_id, op["tool"], op["args"], result)
    return {"applied": True, "operation_id": operation_id, "tool": op["tool"], "result": result}


@mcp.tool(title='Cancel Staged Write', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def google_write_cancel(operation_id: str) -> dict:
    """Discard a staged Google Workspace write operation without applying anything."""
    _purge_expired()
    op = _STAGED.pop(operation_id, None)
    if op is None:
        raise RuntimeError(f"Unknown, cancelled, or already-applied operation: {operation_id}")
    _audit_event("cancel", operation_id, op["tool"], args=op.get("args"))
    return {"cancelled": True, "operation_id": operation_id, "tool": op["tool"]}


@mcp.tool(title='List Staged Writes', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def google_write_list_staged() -> list:
    """List all staged (not yet committed/cancelled) Google Workspace write operations."""
    _purge_expired()
    return [
        {"operation_id": oid, "tool": op["tool"], "checks": op["checks"],
         "preview": op["preview"], "staged_at": op["staged_at"]}
        for oid, op in _STAGED.items()
    ]


@mcp.tool(title='Auth Status', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def google_auth_status() -> str:
    """Report whether the Google OAuth token exists and is valid. Use this first."""
    if not TOKEN_PATH.exists():
        return f"NOT_AUTHENTICATED: no token at {TOKEN_PATH}"
    try:
        creds = _get_creds()
    except Exception as exc:  # noqa: BLE001
        return f"AUTH_ERROR: {exc}"
    scopes = getattr(creds, "scopes", None) or []
    return f"AUTHENTICATED: scopes={len(scopes)} valid={creds.valid}"


# ---------------------------------------------------------------- Sheets

@mcp.tool(title='Get Spreadsheet Metadata', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_sheets_metadata(spreadsheet_id: str) -> dict:
    """Get a spreadsheet's full metadata: title, all sheet tabs with sheetId, index,
    gridProperties (row/column counts), sheetType, tab color, hidden state, etc."""
    s = _svc("sheets", "v4")
    return s.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()


@mcp.tool(title='Read Sheet Range', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_sheets_read(spreadsheet_id: str, range_: str) -> dict:
    """Read cells from a spreadsheet. range_ like 'Sheet1!A1:D10' or 'A1:D10'.
    Returns the full response: range, majorDimension, and the values grid."""
    s = _svc("sheets", "v4")
    return s.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=range_).execute()


@mcp.tool(title='Update Sheet Range (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
def google_sheets_update(spreadsheet_id: str, range_: str, values: list) -> dict:
    """STAGED write: overwrite cells in a spreadsheet. values = list of rows, each a list
    of cell values. Returns a preview (current vs new values) + operation_id; apply with
    google_write_commit. Refuses to commit if the range changed since staging."""
    s = _svc("sheets", "v4")
    before = s.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=range_).execute().get("values", [])

    def apply():
        body = {"values": values}
        return s.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id, range=range_, valueInputOption="RAW", body=body
        ).execute()

    def revalidate():
        cur = s.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=range_).execute().get("values", [])
        if cur != before:
            raise RuntimeError(
                f"Range {range_} changed since staging; refusing to overwrite. Re-stage the update."
            )

    return _stage("google_sheets_update", {"spreadsheet_id": spreadsheet_id, "range": range_, "values": values},
                  apply, [f"range {range_} read OK, {len(before)} row(s) currently present"],
                  {"range": range_, "before": before, "after": values}, revalidate)


@mcp.tool(title='Append Sheet Rows (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def google_sheets_append(spreadsheet_id: str, range_: str, values: list) -> dict:
    """STAGED write: append rows to a spreadsheet. values = list of rows, each a list of cell values.
    Returns a preview (rows to add + current row count) + operation_id; apply with google_write_commit."""
    s = _svc("sheets", "v4")
    current = s.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=range_).execute().get("values", [])

    def apply():
        body = {"values": values}
        return s.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id, range=range_, valueInputOption="RAW",
            insertDataOption="INSERT_ROWS", body=body,
        ).execute()

    return _stage("google_sheets_append", {"spreadsheet_id": spreadsheet_id, "range": range_, "values": values},
                  apply, [f"range {range_} read OK, {len(current)} row(s) present"],
                  {"range": range_, "current_row_count": len(current), "rows_to_append": values}, None)


@mcp.tool(title='Create Spreadsheet (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def google_sheets_create(title: str, sheet_name: str = "Sheet1") -> dict:
    """STAGED write: create a new spreadsheet with the given title and one tab.
    Returns a preview + operation_id; apply with google_write_commit."""
    s = _svc("sheets", "v4")

    def apply():
        body = {"properties": {"title": title}, "sheets": [{"properties": {"title": sheet_name}}]}
        return s.spreadsheets().create(body=body).execute()

    return _stage("google_sheets_create", {"title": title, "sheet_name": sheet_name},
                  apply, ["title non-empty"], {"title": title, "first_tab": sheet_name}, None)


# ---------------------------------------------------------------- Drive

@mcp.tool(title='Search Drive', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_drive_search(query: str = "", max_results: int = 10) -> list:
    """Search Drive files. query is a Drive API query (e.g. "name contains 'NDA'"), empty = recent files.
    Returns full file resources (id, name, mimeType, size, createdTime, modifiedTime,
    trashed, capabilities, owners, parents, webViewLink, ...)."""
    d = _svc("drive", "v3")
    q = query or None
    resp = d.files().list(
        q=q, pageSize=min(max_results, 100),
        fields="nextPageToken,files(id,name,mimeType,size,createdTime,modifiedTime,"
               "trashed,capabilities,owners,parents,webViewLink,iconLink,"
               "hasThumbnail,thumbnailLink,shared,starred,viewedByMeTime)",
        orderBy="modifiedTime desc",
    ).execute()
    return resp.get("files", [])


@mcp.tool(title='Get Drive File Metadata', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_drive_get(file_id: str) -> dict:
    """Get the FULL metadata resource for one Drive file or folder by ID
    (size, createdTime, trashed, capabilities, owners, permissions info, etc.)."""
    d = _svc("drive", "v3")
    return d.files().get(fileId=file_id, fields="*").execute()


@mcp.tool(title='Download Drive File', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_drive_download(file_id: str, export_mime: str = "") -> dict:
    """Download a Drive file to the local download dir. Google-native files export
    (export_mime overrides, e.g. 'application/pdf', 'text/plain', 'text/csv'); binaries download as-is.
    Returns local path, name, mimeType, and size."""
    d = _svc("drive", "v3")
    meta = d.files().get(fileId=file_id, fields="name,mimeType").execute()
    name = meta.get("name", file_id)
    mime = meta.get("mimeType", "")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in name if c not in '\\/:*?"<>|').strip() or file_id
    if mime.startswith("application/vnd.google-apps"):
        target = export_mime or {
            "application/vnd.google-apps.document": "application/pdf",
            "application/vnd.google-apps.spreadsheet": "text/csv",
            "application/vnd.google-apps.slides": "application/pdf",
            "application/vnd.google-apps.drawing": "image/png",
        }.get(mime, "application/pdf")
        ext = {  # noqa: SIM115 - mapping only
            "application/pdf": "pdf", "text/plain": "txt", "text/csv": "csv",
            "image/png": "png", "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        }.get(target, "bin")
        out = DOWNLOAD_DIR / f"{safe}.{ext}"
        req = d.files().export_media(fileId=file_id, mimeType=target)
    else:
        out = DOWNLOAD_DIR / safe
        req = d.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(io.FileIO(str(out), "wb"), req, chunksize=8 * 1024 * 1024)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return {"path": str(out), "name": name, "mimeType": mime, "size": out.stat().st_size,
            "exported": mime.startswith("application/vnd.google-apps")}


@mcp.tool(title='Upload to Drive (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def google_drive_upload(local_path: str, name: str = "", parent_folder_id: str = "") -> dict:
    """STAGED write: upload a local file to Drive (optionally into a folder).
    Returns a preview (file, target name, parent) + operation_id; apply with google_write_commit."""
    p = Path(local_path)
    if not p.exists():
        raise RuntimeError(f"Local file not found: {local_path}")
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    size = p.stat().st_size
    mtime = p.stat().st_mtime
    if parent_folder_id:
        try:
            parent = _svc("drive", "v3").files().get(
                fileId=parent_folder_id, fields="id,name,mimeType,trashed").execute()
            if parent.get("trashed"):
                raise RuntimeError(f"Parent folder {parent_folder_id} is trashed.")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Parent folder {parent_folder_id} not accessible: {exc}") from exc

    def apply():
        d = _svc("drive", "v3")
        body = {"name": name or p.name}
        if parent_folder_id:
            body["parents"] = [parent_folder_id]
        media = MediaFileUpload(str(p), resumable=True, chunksize=8 * 1024 * 1024)
        req = d.files().create(body=body, media_body=media, fields="*")
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
        return resp

    def revalidate():
        if not p.exists():
            raise RuntimeError(f"Local file disappeared since staging: {local_path}")
        if hashlib.sha256(p.read_bytes()).hexdigest() != digest or p.stat().st_mtime != mtime:
            raise RuntimeError("Local file changed since staging; refusing to upload. Re-stage.")

    return _stage("google_drive_upload", {"local_path": local_path, "name": name or p.name,
                                          "parent_folder_id": parent_folder_id},
                  apply, [f"local file exists ({size} bytes, sha256:{digest[:16]})"],
                  {"local_path": str(p), "target_name": name or p.name,
                   "parent_folder_id": parent_folder_id, "size": size,
                   "sha256": digest[:16]}, revalidate)


@mcp.tool(title='Create Drive Folder (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def google_drive_create_folder(name: str, parent_folder_id: str = "") -> dict:
    """STAGED write: create a Drive folder (optionally inside another folder).
    Returns a preview + operation_id; apply with google_write_commit."""
    d = _svc("drive", "v3")

    def apply():
        body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent_folder_id:
            body["parents"] = [parent_folder_id]
        return d.files().create(body=body, fields="*").execute()

    return _stage("google_drive_create_folder", {"name": name, "parent_folder_id": parent_folder_id},
                  apply, ["name non-empty"], {"name": name, "parent_folder_id": parent_folder_id}, None)


@mcp.tool(title='Share Drive File (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
def google_drive_share(file_id: str, email: str = "", role: str = "reader") -> dict:
    """STAGED write: share a Drive file. email empty = anyone with the link; role: reader/writer/commenter.
    Returns a preview (current permissions) + operation_id; apply with google_write_commit.
    Refuses to commit if the file disappeared since staging."""
    d = _svc("drive", "v3")
    if role not in {"reader", "writer", "commenter"}:
        raise RuntimeError(f"Invalid role '{role}'; use reader, writer, or commenter.")
    try:
        existing = d.permissions().list(fileId=file_id, fields="permissions(id,type,role,emailAddress)").execute()
    except Exception as exc:
        raise RuntimeError(f"File {file_id} not accessible: {exc}") from exc

    def apply():
        body = {"role": role, "type": "anyone" if not email else "user"}
        if email:
            body["emailAddress"] = email
        return d.permissions().create(fileId=file_id, body=body, fields="*").execute()

    def revalidate():
        d.files().get(fileId=file_id, fields="id").execute()  # 404 -> refuse commit

    return _stage("google_drive_share", {"file_id": file_id, "email": email, "role": role},
                  apply, ["role valid", "file exists and is accessible"],
                  {"file_id": file_id, "new_permission": {"type": "anyone" if not email else "user",
                                                          "email": email, "role": role},
                   "current_permissions": existing.get("permissions", [])}, revalidate)


@mcp.tool(title='Trash Drive File (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
def google_drive_trash(file_id: str) -> dict:
    """STAGED write: move a Drive file or folder to trash (recoverable). Returns a preview
    (file being trashed) + operation_id; apply with google_write_commit. Refuses to stage
    if the file is already trashed, and refuses to commit if it changed since staging."""
    d = _svc("drive", "v3")
    meta = d.files().get(fileId=file_id, fields="id,name,mimeType,trashed").execute()
    if meta.get("trashed"):
        raise RuntimeError(f"File {file_id} ('{meta.get('name')}') is already in trash.")

    def apply():
        d.files().update(fileId=file_id, body={"trashed": True}).execute()
        return {"trashed": file_id, "name": meta.get("name")}

    def revalidate():
        cur = d.files().get(fileId=file_id, fields="trashed").execute()
        if cur.get("trashed"):
            raise RuntimeError("File was already trashed since staging; refusing.")

    return _stage("google_drive_trash", {"file_id": file_id},
                  apply, ["file exists and is not trashed"],
                  {"file_id": file_id, "name": meta.get("name"), "mimeType": meta.get("mimeType")},
                  revalidate)


# ---------------------------------------------------------------- Docs

def _doc_text(document: dict) -> str:
    out = []
    for el in document.get("body", {}).get("content", []):
        if "paragraph" in el:
            for run in el["paragraph"].get("elements", []):
                tr = run.get("textRun")
                if tr:
                    out.append(tr.get("content", ""))
        elif "table" in el:
            for row in el["table"].get("tableRows", []):
                cells = []
                for cell in row.get("tableCells", []):
                    txt = ""
                    for cel in cell.get("content", []):
                        for run in cel.get("paragraph", {}).get("elements", []):
                            tr = run.get("textRun")
                            if tr:
                                txt += tr.get("content", "")
                    cells.append(txt.strip())
                out.append(" | ".join(cells) + "\n")
    return "".join(out)


@mcp.tool(title='Read Doc', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_docs_read(document_id: str) -> dict:
    """Read a Google Doc: title, revisionId, and the full extracted text (paragraphs + tables).
    (The raw document JSON is enormous; this returns the readable content plus identifiers.)"""
    d = _svc("docs", "v1")
    doc = d.documents().get(documentId=document_id).execute()
    return {
        "document_id": document_id,
        "title": doc.get("title"),
        "revisionId": doc.get("revisionId"),
        "text": _doc_text(doc),
    }


@mcp.tool(title='Create Doc (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def google_docs_create(title: str, body: str = "") -> dict:
    """STAGED write: create a new Google Doc, optionally seeded with body text.
    Returns a preview + operation_id; apply with google_write_commit."""
    d = _svc("docs", "v1")

    def apply():
        doc = d.documents().create(body={"title": title}).execute()
        doc_id = doc.get("documentId")
        if body:
            d.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [{"insertText": {"location": {"index": 1}, "text": body}}]},
            ).execute()
        doc["url"] = f"https://docs.google.com/document/d/{doc_id}/edit"
        return doc

    return _stage("google_docs_create", {"title": title, "body_length": len(body)},
                  apply, ["title non-empty"], {"title": title, "body_chars": len(body)}, None)


@mcp.tool(title='Append to Doc (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def google_docs_append(document_id: str, text: str) -> dict:
    """STAGED write: append text to the end of an existing Google Doc.
    Returns a preview (current length vs text to add) + operation_id; apply with google_write_commit.
    Refuses to commit if the doc disappeared or changed since staging."""
    try:
        doc = _svc("docs", "v1").documents().get(
            documentId=document_id, fields="title,revisionId,body/content").execute()
    except Exception as exc:
        raise RuntimeError(f"Document {document_id} not accessible: {exc}") from exc
    content = doc.get("body", {}).get("content", [])
    end_index = content[-1].get("endIndex", 1) if content else 1
    doc_title = doc.get("title", document_id)
    base_revision = doc.get("revisionId")

    def apply():
        d = _svc("docs", "v1")
        fresh = d.documents().get(
            documentId=document_id, fields="revisionId,body/content").execute()
        fresh_content = fresh.get("body", {}).get("content", [])
        fresh_end = fresh_content[-1].get("endIndex", 1) if fresh_content else 1
        resp = d.documents().batchUpdate(
            documentId=document_id,
            body={"requests": [{"insertText": {"location": {"index": fresh_end - 1}, "text": text}}],
                  "writeControl": {"requiredRevisionId": base_revision}},
        ).execute()
        replies = resp.get("replies", [{}])
        return {"document_id": document_id, "inserted_at": replies[0].get("insertText", {}).get("endIndex"),
                "replies": replies}

    def revalidate():
        cur = _svc("docs", "v1").documents().get(documentId=document_id, fields="revisionId").execute()
        if cur.get("revisionId") != base_revision:
            raise RuntimeError("Document changed since staging; refusing to append at stale offset. Re-stage.")

    return _stage("google_docs_append", {"document_id": document_id, "text_length": len(text)},
                  apply, ["document exists and is accessible"],
                  {"document_id": document_id, "title": doc_title,
                   "current_end_index": end_index, "revision": (base_revision or "")[:8],
                   "text_preview": text[:500], "text_to_append_chars": len(text)}, revalidate)


# ---------------------------------------------------------------- Forms

@mcp.tool(title='List Forms', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_forms_list(max_results: int = 20) -> list:
    """List Google Forms in Drive. Returns full file entries (id, name, mimeType,
    createdTime, modifiedTime, size, webViewLink, ...)."""
    d = _svc("drive", "v3")
    resp = d.files().list(
        q="mimeType='application/vnd.google-apps.form'",
        pageSize=min(max_results, 100),
        fields="files(id,name,mimeType,createdTime,modifiedTime,size,webViewLink)",
        orderBy="modifiedTime desc",
    ).execute()
    return resp.get("files", [])


@mcp.tool(title='Get Form', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_forms_get(form_id: str) -> dict:
    """Get a form's FULL resource: info (title, description), settings
    (emailCollectionType, quiz settings), revisionId, responderUri, and every item —
    question type, required flag, validation rules, choice options, date/time
    config, scale limits, file-upload constraints, section/page structure."""
    f = _svc("forms", "v1")
    return f.forms().get(formId=form_id).execute()


@mcp.tool(title='Read Form Responses', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_forms_responses(form_id: str, max_results: int = 50) -> dict:
    """Read submitted responses of a Google Form. Returns the raw API response:
    each response with responseId, createTime, lastSubmittedTime, respondentEmail,
    totalScore, and full answers (text and file uploads) keyed by question id."""
    f = _svc("forms", "v1")
    return f.forms().responses().list(formId=form_id, pageSize=min(max_results, 500)).execute()


# ---------------------------------------------------------------- Gmail (read)

def _gmail_body(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        return "[html] " + base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")[:2000]
    parts = payload.get("parts", [])
    if parts:
        plain = ""
        for part in parts:
            txt = _gmail_body(part)
            if part.get("mimeType") == "text/plain":
                return txt
            if txt and not plain:
                plain = txt
        return plain
    return ""


def _gmail_attachments(payload: dict) -> list:
    out = []
    if payload.get("body", {}).get("attachmentId"):
        out.append({
            "filename": payload.get("filename"),
            "mimeType": payload.get("mimeType"),
            "attachmentId": payload["body"]["attachmentId"],
            "size": payload.get("body", {}).get("size"),
        })
    for part in payload.get("parts", []):
        out.extend(_gmail_attachments(part))
    return out


@mcp.tool(title='Search Gmail', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_gmail_search(query: str = "", max_results: int = 10) -> list:
    """Search Gmail (read-only). query = Gmail search syntax, e.g. 'is:unread' or 'from:x newer_than:1d'.
    Returns full metadata per message: all headers, labelIds, snippet, internalDate."""
    g = _svc("gmail", "v1")
    resp = g.users().messages().list(userId="me", q=query or None, maxResults=min(max_results, 50)).execute()
    ids = [m["id"] for m in resp.get("messages", [])[:_MAX_GMAIL_FANOUT]]
    got: dict = {}

    def _cb(request_id, response, exception):
        got[request_id] = (response, exception)

    batch = g.new_batch_http_request(callback=_cb)
    for mid in ids:
        batch.add(g.users().messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=["From", "To", "Cc", "Bcc", "Subject", "Date", "Reply-To"],
            fields="id,threadId,labelIds,snippet,internalDate,payload/headers"),
            request_id=mid)
    _exec(batch, kind="read", timeout=_READ_TIMEOUT)
    out = []
    for mid in ids:
        response, exception = got.get(mid, (None, None))
        if exception is not None or response is None:
            continue
        full = response
        headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
        out.append({"id": mid, "threadId": full.get("threadId"),
                    "headers": headers, "labelIds": full.get("labelIds"),
                    "snippet": full.get("snippet"), "internalDate": full.get("internalDate")})
    return out


@mcp.tool(title='Get Gmail Message', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_gmail_get(message_id: str) -> dict:
    """Read one Gmail message: ALL headers, labels, snippet, internalDate, sizeEstimate,
    plain-text body (html fallback), and the attachment list (id, filename, mimeType, size)."""
    g = _svc("gmail", "v1")
    full = g.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
    return {
        "id": message_id,
        "threadId": full.get("threadId"),
        "headers": headers,
        "labelIds": full.get("labelIds"),
        "snippet": full.get("snippet"),
        "internalDate": full.get("internalDate"),
        "sizeEstimate": full.get("sizeEstimate"),
        "attachments": _gmail_attachments(full.get("payload", {})),
        "body": _gmail_body(full.get("payload", {})),
    }


# ---------------------------------------------------------------- Calendar

@mcp.tool(title='List Calendar Events', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_calendar_list(start: str = "", end: str = "", max_results: int = 25) -> list:
    """List calendar events. start/end = ISO 8601 (e.g. '2026-08-11T00:00:00Z'); empty start = now, empty end = +7 days.
    Returns the FULL event resources (attendees, status, description, reminders, ...)."""
    c = _svc("calendar", "v3")
    if not start:
        start = datetime.now(timezone.utc).isoformat()
    if not end:
        end = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    resp = c.events().list(
        calendarId="primary", timeMin=start, timeMax=end,
        singleEvents=True, orderBy="startTime", maxResults=min(max_results, 250),
    ).execute()
    return resp.get("items", [])


@mcp.tool(title='Get Calendar Event', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_calendar_get(event_id: str) -> dict:
    """Get one calendar event by ID. Returns the FULL event resource
    (attendees, organizer, status, description, reminders, attachments, ...)."""
    c = _svc("calendar", "v3")
    return c.events().get(calendarId="primary", eventId=event_id).execute()


@mcp.tool(title='Create Calendar Event (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def google_calendar_create(summary: str, start: str, end: str, description: str = "",
                           location: str = "", attendees: str = "") -> dict:
    """STAGED write: create a calendar event. start/end = ISO 8601 WITH timezone
    (e.g. '2026-08-11T09:00:00+08:00'). attendees = comma-separated emails.
    Returns a preview + operation_id; apply with google_write_commit."""
    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError:
        raise RuntimeError(f"start/end must be ISO 8601 with timezone (got {start!r} / {end!r}).")
    if start_dt.tzinfo is None or end_dt.tzinfo is None:
        raise RuntimeError("start/end must include a timezone offset.")
    if end_dt <= start_dt:
        raise RuntimeError("end must be after start.")
    attendee_list = [a.strip() for a in attendees.split(",") if a.strip()]
    ical_uid = f"{secrets.token_hex(8)}@mcps"

    def apply():
        c = _svc("calendar", "v3")
        body = {"summary": summary, "start": {"dateTime": start}, "end": {"dateTime": end},
                "iCalUID": ical_uid}
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendee_list:
            body["attendees"] = [{"email": a} for a in attendee_list]
        return c.events().insert(calendarId="primary", body=body).execute()

    return _stage("google_calendar_create",
                  {"summary": summary, "start": start, "end": end, "attendees": attendees},
                  apply,
                  ["start/end valid ISO8601 with tz", "end after start"],
                  {"summary": summary, "start": start, "end": end,
                   "description": description[:500], "location": location,
                   "attendees": attendee_list}, None)


@mcp.tool(title='Delete Calendar Event (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
def google_calendar_delete(event_id: str) -> dict:
    """STAGED write: delete a calendar event by ID. Returns a preview (the event being
    deleted) + operation_id; apply with google_write_commit. Refuses to stage if the
    event does not exist, and refuses to commit if it was deleted meanwhile."""
    c = _svc("calendar", "v3")
    try:
        ev = c.events().get(calendarId="primary", eventId=event_id).execute()
    except Exception as exc:
        raise RuntimeError(f"Event {event_id} not found or not accessible: {exc}") from exc

    def apply():
        c.events().delete(calendarId="primary", eventId=event_id).execute()
        return {"deleted": event_id}

    def revalidate():
        c.events().get(calendarId="primary", eventId=event_id).execute()  # 404 -> refuse

    return _stage("google_calendar_delete", {"event_id": event_id},
                  apply, ["event exists"],
                  {"event_id": event_id, "summary": ev.get("summary"),
                   "start": ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date"),
                   "status": ev.get("status")}, revalidate)


# ---------------------------------------------------------------- People

@mcp.tool(title='List Contacts', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_people_contacts(max_results: int = 100) -> list:
    """List the user's Google contacts. Returns the RAW person resources with all
    requested fields: names, emails, phones, organizations, addresses, birthdays,
    memberships, urls, biographies, user-defined fields, and metadata."""
    p = _svc("people", "v1")
    resp = p.people().connections().list(
        resourceName="people/me",
        pageSize=min(max_results, 1000),
        personFields="names,emailAddresses,phoneNumbers,organizations,addresses,"
                     "birthdays,memberships,urls,userDefined,biographies,metadata",
    ).execute()
    return resp.get("connections", [])


# ---------------------------------------------------------------- Slides

def _slide_text(page_element: dict) -> str:
    shape = page_element.get("shape")
    if not shape:
        return ""
    return " ".join(
        te.get("textRun", {}).get("content", "").strip()
        for te in shape.get("text", {}).get("textElements", [])
        if te.get("textRun", {}).get("content", "").strip()
    )


@mcp.tool(title='Get Presentation', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_slides_get(presentation_id: str) -> dict:
    """Read a Google Slides presentation: title, slide count, and per-slide detail —
    layout, extracted text, speaker notes, and element inventory (objectId + type)."""
    s = _svc("slides", "v1")
    pres = s.presentations().get(presentationId=presentation_id).execute()
    slides = []
    for sl in pres.get("slides", []):
        elements = []
        texts = []
        for el in sl.get("pageElements", []):
            kind = "shape" if "shape" in el else next(iter(k for k in
                   ("image", "line", "video", "table", "group", "wordArt", "sheetsChart", "placeholder")
                   if k in el), "unknown")
            elements.append({"objectId": el.get("objectId"), "type": kind})
            txt = _slide_text(el)
            if txt:
                texts.append(txt)
        notes = []
        for el in sl.get("slideProperties", {}).get("notesPage", {}).get("pageElements", []):
            txt = _slide_text(el)
            if txt:
                notes.append(txt)
        slides.append({
            "id": sl.get("objectId"),
            "layout": sl.get("slideProperties", {}).get("layoutObjectId"),
            "text": " ".join(texts),
            "notes": " ".join(notes),
            "elements": elements,
        })
    return {"title": pres.get("title"), "slide_count": len(slides), "slides": slides}


@mcp.tool(title='Create Slides (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def google_slides_create(title: str) -> dict:
    """STAGED write: create a new Google Slides presentation with the given title.
    Returns a preview + operation_id; apply with google_write_commit."""
    s = _svc("slides", "v1")

    def apply():
        pres = s.presentations().create(body={"title": title}).execute()
        pid = pres.get("presentationId")
        pres["url"] = f"https://docs.google.com/presentation/d/{pid}/edit"
        return pres

    return _stage("google_slides_create", {"title": title},
                  apply,
                  ["title non-empty"],
                  {"title": title}, None)


# ---------------------------------------------------------------- Tasks

@mcp.tool(title='List Task Lists', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_tasks_lists() -> list:
    """List the user's task lists. Returns full task list resources (id, title, updated, ...)."""
    t = _svc("tasks", "v1")
    resp = t.tasklists().list(maxResults=100).execute()
    return resp.get("items", [])


@mcp.tool(title='List Tasks', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_tasks_list(tasklist_id: str, max_results: int = 50) -> list:
    """List tasks in a task list. tasklist_id = '@default' for the default list.
    Returns full task resources (id, title, status, due, notes, completed, deleted,
    hidden, position, updated, links, ...)."""
    t = _svc("tasks", "v1")
    resp = t.tasks().list(tasklist=tasklist_id, maxResults=min(max_results, 100)).execute()
    return resp.get("items", [])


@mcp.tool(title='Create Task (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def google_tasks_create(tasklist_id: str, title: str, notes: str = "", due: str = "") -> dict:
    """STAGED write: create a task. tasklist_id = '@default' for the default list.
    due = ISO 8601 (e.g. '2026-08-20T00:00:00.000Z'). Returns a preview + operation_id;
    apply with google_write_commit."""
    t = _svc("tasks", "v1")

    def apply():
        body = {"title": title}
        if notes:
            body["notes"] = notes
        if due:
            body["due"] = due
        return t.tasks().insert(tasklist=tasklist_id, body=body).execute()

    return _stage("google_tasks_create", {"tasklist_id": tasklist_id, "title": title,
                                          "notes": notes, "due": due},
                  apply,
                  ["title non-empty"],
                  {"tasklist_id": tasklist_id, "title": title, "notes": notes, "due": due}, None)


@mcp.tool(title='Update Task Status (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
def google_tasks_update(tasklist_id: str, task_id: str, status: str = "completed",
                        title: str = "", notes: str = "", due: str = "") -> dict:
    """STAGED write: update a task (status, and optionally title/notes/due).
    Uses PATCH (never full-replace). Returns a preview (current vs requested)
    + operation_id; apply with google_write_commit. Refuses to commit if the
    task changed since staging."""
    if status not in {"completed", "needsAction"}:
        raise RuntimeError(f"Invalid status '{status}'; use 'completed' or 'needsAction'.")
    try:
        tk = _svc("tasks", "v1").tasks().get(tasklist=tasklist_id, task=task_id).execute()
    except Exception as exc:
        raise RuntimeError(f"Task {task_id} not found: {exc}") from exc
    patch = {"id": task_id, "status": status}
    if title:
        patch["title"] = title
    if notes:
        patch["notes"] = notes
    if due:
        patch["due"] = due
    base_etag, base_updated = tk.get("etag"), tk.get("updated")

    def apply():
        t = _svc("tasks", "v1")
        return t.tasks().patch(tasklist=tasklist_id, task=task_id, body=patch).execute()

    def revalidate():
        cur = _svc("tasks", "v1").tasks().get(tasklist=tasklist_id, task=task_id).execute()
        if cur.get("etag") != base_etag or cur.get("updated") != base_updated:
            raise RuntimeError("Task changed since staging; refusing to overwrite. Re-stage.")

    return _stage("google_tasks_update",
                  {"tasklist_id": tasklist_id, "task_id": task_id, "status": status,
                   "title": title, "notes": notes, "due": due},
                  apply, ["status valid", "task exists"],
                  {"task_id": task_id, "title": tk.get("title"), "notes": tk.get("notes"),
                   "due": tk.get("due"), "current_status": tk.get("status"),
                   "requested": {k: v for k, v in patch.items() if k != "id"}}, revalidate)


@mcp.tool(title='Delete Task (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
def google_tasks_delete(tasklist_id: str, task_id: str) -> dict:
    """STAGED write: delete a task permanently. Returns a preview (the task being deleted)
    + operation_id; apply with google_write_commit. Refuses to stage if the task does not
    exist, and refuses to commit if it was deleted meanwhile."""
    t = _svc("tasks", "v1")
    try:
        tk = t.tasks().get(tasklist=tasklist_id, task=task_id).execute()
    except Exception as exc:
        raise RuntimeError(f"Task {task_id} not found: {exc}") from exc

    def apply():
        t.tasks().delete(tasklist=tasklist_id, task=task_id).execute()
        return {"deleted": task_id}

    def revalidate():
        t.tasks().get(tasklist=tasklist_id, task=task_id).execute()  # 404 -> refuse

    return _stage("google_tasks_delete", {"tasklist_id": tasklist_id, "task_id": task_id},
                  apply, ["task exists"],
                  {"task_id": task_id, "title": tk.get("title"), "status": tk.get("status")},
                  revalidate)


# ---------------------------------------------------------------- Chat

@mcp.tool(title='List Chat Spaces', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_chat_spaces(max_results: int = 20) -> list:
    """List Google Chat spaces the user belongs to. Returns full space resources
    (name, displayName, spaceType, spaceThreadingState, adminInstalled, ...)."""
    ch = _svc("chat", "v1")
    resp = ch.spaces().list(pageSize=min(max_results, 100)).execute()
    return resp.get("spaces", [])


@mcp.tool(title='List Chat Messages', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_chat_messages(space_name: str, max_results: int = 20) -> list:
    """List recent messages in a Chat space. space_name like 'spaces/AAAA...'.
    Returns full message resources (text, formattedText, sender, createTime, thread, attachment, ...)."""
    ch = _svc("chat", "v1")
    resp = ch.spaces().messages().list(parent=space_name, pageSize=min(max_results, 100)).execute()
    return resp.get("messages", [])


@mcp.tool(title='Send Chat Message (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def google_chat_send(space_name: str, text: str) -> dict:
    """STAGED write: send a message to a Chat space the user is in. space_name like 'spaces/AAAA...'.
    Returns a preview (space + exact message text) + operation_id; apply with google_write_commit."""
    if not text.strip():
        raise RuntimeError("Message text must be non-empty.")
    try:
        space = _svc("chat", "v1").spaces().get(name=space_name).execute()
    except Exception as exc:
        raise RuntimeError(f"Chat space {space_name} not accessible: {exc}") from exc

    def apply():
        ch = _svc("chat", "v1")
        return ch.spaces().messages().create(
            parent=space_name,
            body={"text": text},
            messageId=f"client-{secrets.token_hex(8)}").execute()

    def revalidate():
        _svc("chat", "v1").spaces().get(name=space_name).execute()  # 404 -> refuse

    return _stage("google_chat_send", {"space_name": space_name, "text": text},
                  apply,
                  ["text non-empty", f"space exists ({space.get('displayName', space_name)})"],
                  {"space_name": space_name, "space_display": space.get("displayName", ""),
                   "text": text, "text_chars": len(text)}, revalidate)


# ---------------------------------------------------------------- Meet

@mcp.tool(title='Create Meet Space (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def google_meet_create_space() -> dict:
    """STAGED write: create a new Google Meet video conference space.
    Returns a preview + operation_id; apply with google_write_commit."""
    mt = _svc("meet", "v2")

    def apply():
        return mt.spaces().create(body={}).execute()

    return _stage("google_meet_create_space", {},
                  apply,
                  ["no args"],
                  {"note": "Creates a brand-new Meet space (a real meeting link)."}, None)


@mcp.tool(title='Get Meet Space', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_meet_get_space(space_name: str) -> dict:
    """Get details for a Meet space. space_name like 'spaces/ABC-XYZ123'.
    Returns the full space resource (name, meetingUri, meetingCode, config, activeConference)."""
    mt = _svc("meet", "v2")
    return mt.spaces().get(name=space_name).execute()


# ---------------------------------------------------------------- v2 additions

@mcp.tool(title='Get Gmail Thread', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_gmail_thread_get(thread_id: str) -> dict:
    """Get a full Gmail conversation thread in one call (id + messages with
    headers, snippet, labels). Read-only."""
    g = _svc("gmail", "v1")
    th = g.users().threads().get(userId="me", id=thread_id, format="metadata",
                                 metadataHeaders=["From", "Subject", "Date"]).execute()
    return {"id": th.get("id"), "historyId": th.get("historyId"),
            "messages": [{"id": m.get("id"),
                          "headers": {h["name"]: h["value"] for h in m.get("payload", {}).get("headers", [])},
                          "labelIds": m.get("labelIds"), "snippet": m.get("snippet"),
                          "internalDate": m.get("internalDate")}
                         for m in th.get("messages", [])]}


@mcp.tool(title='Download Gmail Attachment', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_gmail_attachment_download(message_id: str, attachment_id: str, filename: str = "") -> dict:
    """Download a Gmail attachment to the local download dir (mirrors drive_download).
    Returns local path, name, mimeType, and size."""
    g = _svc("gmail", "v1")
    att = g.users().messages().attachments().get(
        userId="me", messageId=message_id, id=attachment_id).execute()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in (filename or attachment_id) if c not in '\\/:*?"<>|').strip() or attachment_id
    out = DOWNLOAD_DIR / safe
    out.write_bytes(base64.urlsafe_b64decode(att.get("data", "")))
    return {"path": str(out), "name": safe, "mimeType": att.get("mimeType", ""),
            "size": out.stat().st_size}


@mcp.tool(title='Search Contacts', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_people_search(query: str, max_results: int = 10) -> list:
    """Search contacts by name/email/org substring (filters the connections list
    client-side; needs no extra scopes). Returns matching full person resources."""
    p = _svc("people", "v1")
    q = (query or "").lower()
    out = []
    token = ""
    while len(out) < max_results:
        resp = p.people().connections().list(
            resourceName="people/me", pageSize=min(200, max(50, max_results * 2)),
            personFields="names,emailAddresses,phoneNumbers,organizations",
            pageToken=token or None).execute()
        for person in resp.get("connections", []):
            blob = json.dumps(person, ensure_ascii=False, default=str).lower()
            if q in blob:
                out.append(person)
                if len(out) >= max_results:
                    break
        token = resp.get("nextPageToken", "")
        if not token:
            break
    return out


@mcp.tool(title='Get Contact', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_people_get(resource_name: str) -> dict:
    """Get one contact by resourceName (e.g. 'people/c123'). Returns the full person resource."""
    p = _svc("people", "v1")
    return p.people().get(resourceName=resource_name,
                          personFields="names,emailAddresses,phoneNumbers,organizations,addresses,"
                                       "birthdays,memberships,urls,userDefined,biographies,metadata").execute()


@mcp.tool(title='Get Task', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_tasks_get(tasklist_id: str, task_id: str) -> dict:
    """Get one task by ID. Returns the full task resource (title, notes, due, status, etag...)."""
    t = _svc("tasks", "v1")
    return t.tasks().get(tasklist=tasklist_id, task=task_id).execute()


@mcp.tool(title='List Drive Permissions', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_drive_permissions(file_id: str) -> dict:
    """Audit who a Drive file is shared with. Returns file_id + permission list
    (id, type, role, emailAddress, displayName, expirationTime)."""
    d = _svc("drive", "v3")
    perms = d.permissions().list(
        fileId=file_id,
        fields="permissions(id,type,role,emailAddress,displayName,expirationTime)").execute()
    return {"file_id": file_id, "permissions": perms.get("permissions", [])}


@mcp.tool(title='List Chat Members', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_chat_members(space_name: str, max_results: int = 50) -> list:
    """List members of a Chat space (who is in the room before posting).
    Returns membership resources (member name/type, roles)."""
    ch = _svc("chat", "v1")
    resp = ch.spaces().members().list(parent=space_name, pageSize=min(max_results, 100)).execute()
    return resp.get("memberships", [])


@mcp.tool(title='Query Free/Busy', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_calendar_freebusy(time_min: str, time_max: str, calendar_ids: list | None = None) -> dict:
    """Query free/busy windows (e.g. 'when is X free?'). time_min/max ISO 8601.
    Returns the raw freebusy response (calendars + groups busy blocks)."""
    c = _svc("calendar", "v3")
    body = {"timeMin": time_min, "timeMax": time_max,
            "items": [{"id": cid} for cid in (calendar_ids or ["primary"])]}
    return c.freebusy().query(body=body).execute()


@mcp.tool(title='Get Conditional Formats', annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def google_sheets_conditional_formats(spreadsheet_id: str) -> dict:
    """List conditional-format rules per tab of a spreadsheet (promotes the
    sidecar probe to a first-class tool). Returns sheets with sheetId, title, rules."""
    s = _svc("sheets", "v4")
    res = s.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(sheetId,title),conditionalFormats)").execute()
    return {"spreadsheet_id": spreadsheet_id,
            "sheets": [{"sheetId": sh["properties"]["sheetId"], "title": sh["properties"]["title"],
                        "rules": sh.get("conditionalFormats", [])}
                       for sh in res.get("sheets", [])]}


@mcp.tool(title='Send Gmail (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def google_gmail_send(to: str, subject: str, body: str, cc: str = "", bcc: str = "") -> dict:
    """STAGED write: send an email via Gmail (plain text). to/cc/bcc are
    comma-separated emails. Returns a preview + operation_id; apply with
    google_write_commit."""
    recipients = [a.strip() for a in to.split(",") if a.strip()]
    if not recipients:
        raise RuntimeError("At least one 'to' recipient is required.")
    if not subject.strip():
        raise RuntimeError("Subject must be non-empty.")
    if not body.strip():
        raise RuntimeError("Body must be non-empty.")

    def apply():
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        if cc.strip():
            msg["Cc"] = cc.strip()
        if bcc.strip():
            msg["Bcc"] = bcc.strip()
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        return _svc("gmail", "v1").users().messages().send(
            userId="me", body={"raw": raw}).execute()

    return _stage("google_gmail_send",
                  {"to": to, "subject": subject, "cc": cc, "bcc": bcc},
                  apply, ["recipients parse", "subject non-empty", "body non-empty"],
                  {"to": recipients, "cc": cc, "bcc": ("<set>" if bcc.strip() else ""),
                   "subject": subject, "body_preview": body[:500],
                   "body_chars": len(body)}, None)


@mcp.tool(title='Patch Calendar Event (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
def google_calendar_patch(event_id: str, summary: str = "", start: str = "", end: str = "",
                          description: str = "", location: str = "") -> dict:
    """STAGED write: reschedule or edit an event (only the given fields change).
    Empty string = no change. Returns a preview + operation_id; apply with
    google_write_commit. Refuses to commit if the event changed since staging."""
    try:
        ev = _svc("calendar", "v3").events().get(calendarId="primary", eventId=event_id).execute()
    except Exception as exc:
        raise RuntimeError(f"Event {event_id} not found: {exc}") from exc
    patch: dict = {}
    if summary:
        patch["summary"] = summary
    if start:
        patch["start"] = {"dateTime": start}
    if end:
        patch["end"] = {"dateTime": end}
    if description:
        patch["description"] = description
    if location:
        patch["location"] = location
    if not patch:
        raise RuntimeError("Nothing to change: pass at least one of summary/start/end/description/location.")
    base_etag, base_updated = ev.get("etag"), ev.get("updated")

    def apply():
        c = _svc("calendar", "v3")
        return c.events().patch(calendarId="primary", eventId=event_id, body=patch).execute()

    def revalidate():
        cur = _svc("calendar", "v3").events().get(calendarId="primary", eventId=event_id).execute()
        if cur.get("etag") != base_etag or cur.get("updated") != base_updated:
            raise RuntimeError("Event changed since staging; refusing to overwrite. Re-stage.")

    return _stage("google_calendar_patch", {"event_id": event_id, **patch},
                  apply, ["event exists", "at least one field"],
                  {"event_id": event_id, "current_summary": ev.get("summary"),
                   "current_start": (ev.get("start") or {}).get("dateTime"),
                   "requested": patch}, revalidate)


@mcp.tool(title='Copy Drive File (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def google_drive_copy(file_id: str, name: str = "", parent_folder_id: str = "") -> dict:
    """STAGED write: copy a Drive file (template-copy, organize). Returns a
    preview + operation_id; apply with google_write_commit."""
    try:
        src = _svc("drive", "v3").files().get(
            fileId=file_id, fields="id,name,mimeType,trashed").execute()
    except Exception as exc:
        raise RuntimeError(f"File {file_id} not accessible: {exc}") from exc
    if src.get("trashed"):
        raise RuntimeError(f"File {file_id} is trashed.")

    def apply():
        d = _svc("drive", "v3")
        body: dict = {}
        if name:
            body["name"] = name
        if parent_folder_id:
            body["parents"] = [parent_folder_id]
        return d.files().copy(fileId=file_id, body=body, fields="*").execute()

    return _stage("google_drive_copy", {"file_id": file_id, "name": name,
                                        "parent_folder_id": parent_folder_id},
                  apply, ["source exists and is not trashed"],
                  {"source": {"id": file_id, "name": src.get("name")},
                   "new_name": name or "(same)", "parent_folder_id": parent_folder_id}, None)


@mcp.tool(title='Update Drive File (staged)', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
def google_drive_update(file_id: str, name: str = "", add_parents: str = "",
                        remove_parents: str = "") -> dict:
    """STAGED write: rename and/or move a Drive file (addParents/removeParents
    are comma-separated folder IDs). Returns a preview + operation_id; apply
    with google_write_commit. Refuses to commit if the file changed since staging."""
    try:
        cur = _svc("drive", "v3").files().get(
            fileId=file_id, fields="id,name,mimeType,trashed,modifiedTime,parents").execute()
    except Exception as exc:
        raise RuntimeError(f"File {file_id} not accessible: {exc}") from exc
    if not name and not add_parents and not remove_parents:
        raise RuntimeError("Nothing to change: pass name and/or add_parents/remove_parents.")
    base_modified = cur.get("modifiedTime")

    def apply():
        d = _svc("drive", "v3")
        body: dict = {}
        if name:
            body["name"] = name
        params: dict = {"fileId": file_id, "body": body or None, "fields": "*"}
        if add_parents:
            params["addParents"] = add_parents
        if remove_parents:
            params["removeParents"] = remove_parents
        return d.files().update(**{k: v for k, v in params.items() if v is not None}).execute()

    def revalidate():
        now = _svc("drive", "v3").files().get(fileId=file_id, fields="id,trashed,modifiedTime").execute()
        if now.get("trashed") or now.get("modifiedTime") != base_modified:
            raise RuntimeError("File changed since staging; refusing to overwrite. Re-stage.")

    return _stage("google_drive_update",
                  {"file_id": file_id, "name": name,
                   "add_parents": add_parents, "remove_parents": remove_parents},
                  apply, ["file exists", "at least one change"],
                  {"file_id": file_id, "current_name": cur.get("name"),
                   "requested_name": name or "(unchanged)",
                   "add_parents": add_parents, "remove_parents": remove_parents}, revalidate)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
