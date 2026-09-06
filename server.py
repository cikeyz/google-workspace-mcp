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
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

mcp = FastMCP("google-workspace")


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


def _get_creds() -> Credentials:
    if not TOKEN_PATH.exists():
        raise RuntimeError(
            f"No token at {TOKEN_PATH}. Run the google-workspace skill setup "
            "(setup.py --auth-url, then --auth-code) first."
        )
    info = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    creds = Credentials.from_authorized_user_info(info, scopes=info.get("scopes"))
    if creds.valid:
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
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _svc(name: str, version: str):
    return build(name, version, credentials=_get_creds(), cache_discovery=False)


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


def _audit(op_id: str, tool: str, args: dict, result) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "operation_id": op_id,
            "tool": tool,
            "args": json.dumps(args, ensure_ascii=False, default=str)[:800],
            "result": json.dumps(result, ensure_ascii=False, default=str)[:400],
        }
        with open(AUDIT_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # audit must never break the flow


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


@mcp.tool()
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
        op["revalidate"]()
    result = op["apply"]()
    _audit(operation_id, op["tool"], op["args"], result)
    return {"applied": True, "operation_id": operation_id, "tool": op["tool"], "result": result}


@mcp.tool()
def google_write_cancel(operation_id: str) -> dict:
    """Discard a staged Google Workspace write operation without applying anything."""
    _purge_expired()
    op = _STAGED.pop(operation_id, None)
    if op is None:
        raise RuntimeError(f"Unknown, cancelled, or already-applied operation: {operation_id}")
    return {"cancelled": True, "operation_id": operation_id, "tool": op["tool"]}


@mcp.tool()
def google_write_list_staged() -> list:
    """List all staged (not yet committed/cancelled) Google Workspace write operations."""
    _purge_expired()
    return [
        {"operation_id": oid, "tool": op["tool"], "checks": op["checks"],
         "preview": op["preview"], "staged_at": op["staged_at"]}
        for oid, op in _STAGED.items()
    ]


@mcp.tool()
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

@mcp.tool()
def google_sheets_metadata(spreadsheet_id: str) -> dict:
    """Get a spreadsheet's full metadata: title, all sheet tabs with sheetId, index,
    gridProperties (row/column counts), sheetType, tab color, hidden state, etc."""
    s = _svc("sheets", "v4")
    return s.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()


@mcp.tool()
def google_sheets_read(spreadsheet_id: str, range_: str) -> dict:
    """Read cells from a spreadsheet. range_ like 'Sheet1!A1:D10' or 'A1:D10'.
    Returns the full response: range, majorDimension, and the values grid."""
    s = _svc("sheets", "v4")
    return s.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=range_).execute()


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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

@mcp.tool()
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


@mcp.tool()
def google_drive_get(file_id: str) -> dict:
    """Get the FULL metadata resource for one Drive file or folder by ID
    (size, createdTime, trashed, capabilities, owners, permissions info, etc.)."""
    d = _svc("drive", "v3")
    return d.files().get(fileId=file_id, fields="*").execute()


@mcp.tool()
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
        req = d.files().export(fileId=file_id, mimeType=target)
    else:
        out = DOWNLOAD_DIR / safe
        req = d.files().get_media(fileId=file_id)
    with open(out, "wb") as fh:
        fh.write(req.execute())
    return {"path": str(out), "name": name, "mimeType": mime, "size": out.stat().st_size,
            "exported": mime.startswith("application/vnd.google-apps")}


@mcp.tool()
def google_drive_upload(local_path: str, name: str = "", parent_folder_id: str = "") -> dict:
    """STAGED write: upload a local file to Drive (optionally into a folder).
    Returns a preview (file, target name, parent) + operation_id; apply with google_write_commit."""
    d = _svc("drive", "v3")
    p = Path(local_path)
    if not p.exists():
        raise RuntimeError(f"Local file not found: {local_path}")

    def apply():
        body = {"name": name or p.name}
        if parent_folder_id:
            body["parents"] = [parent_folder_id]
        media = MediaFileUpload(str(p), resumable=False)
        return d.files().create(body=body, media_body=media, fields="*").execute()

    return _stage("google_drive_upload", {"local_path": local_path, "name": name or p.name,
                                          "parent_folder_id": parent_folder_id},
                  apply, [f"local file exists ({p.stat().st_size} bytes)"],
                  {"local_path": str(p), "target_name": name or p.name, "parent_folder_id": parent_folder_id}, None)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def google_docs_append(document_id: str, text: str) -> dict:
    """STAGED write: append text to the end of an existing Google Doc.
    Returns a preview (current length vs text to add) + operation_id; apply with google_write_commit.
    Refuses to commit if the doc disappeared since staging."""
    d = _svc("docs", "v1")
    try:
        doc = d.documents().get(documentId=document_id, fields="title,body/content").execute()
    except Exception as exc:
        raise RuntimeError(f"Document {document_id} not accessible: {exc}") from exc
    content = doc.get("body", {}).get("content", [])
    end_index = content[-1].get("endIndex", 1) if content else 1
    doc_title = doc.get("title", document_id)

    def apply():
        resp = d.documents().batchUpdate(
            documentId=document_id,
            body={"requests": [{"insertText": {"location": {"index": end_index - 1}, "text": text}}]},
        ).execute()
        replies = resp.get("replies", [{}])
        return {"document_id": document_id, "inserted_at": replies[0].get("insertText", {}).get("endIndex"),
                "replies": replies}

    def revalidate():
        d.documents().get(documentId=document_id, fields="documentId").execute()

    return _stage("google_docs_append", {"document_id": document_id, "text_length": len(text)},
                  apply, ["document exists and is accessible"],
                  {"document_id": document_id, "title": doc_title,
                   "current_end_index": end_index, "text_to_append_chars": len(text)}, revalidate)


# ---------------------------------------------------------------- Forms

@mcp.tool()
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


@mcp.tool()
def google_forms_get(form_id: str) -> dict:
    """Get a form's FULL resource: info (title, description), settings
    (emailCollectionType, quiz settings), revisionId, responderUri, and every item —
    question type, required flag, validation rules, choice options, date/time
    config, scale limits, file-upload constraints, section/page structure."""
    f = _svc("forms", "v1")
    return f.forms().get(formId=form_id).execute()


@mcp.tool()
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


@mcp.tool()
def google_gmail_search(query: str = "", max_results: int = 10) -> list:
    """Search Gmail (read-only). query = Gmail search syntax, e.g. 'is:unread' or 'from:x newer_than:1d'.
    Returns full metadata per message: all headers, labelIds, snippet, internalDate."""
    g = _svc("gmail", "v1")
    resp = g.users().messages().list(userId="me", q=query or None, maxResults=min(max_results, 50)).execute()
    out = []
    for m in resp.get("messages", []):
        full = g.users().messages().get(userId="me", id=m["id"], format="metadata",
                                        metadataHeaders=["From", "To", "Cc", "Bcc", "Subject", "Date", "Reply-To"]).execute()
        headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
        out.append({"id": m["id"], "threadId": full.get("threadId"),
                    "headers": headers, "labelIds": full.get("labelIds"),
                    "snippet": full.get("snippet"), "internalDate": full.get("internalDate")})
    return out


@mcp.tool()
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

@mcp.tool()
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


@mcp.tool()
def google_calendar_get(event_id: str) -> dict:
    """Get one calendar event by ID. Returns the FULL event resource
    (attendees, organizer, status, description, reminders, attachments, ...)."""
    c = _svc("calendar", "v3")
    return c.events().get(calendarId="primary", eventId=event_id).execute()


@mcp.tool()
def google_calendar_create(summary: str, start: str, end: str, description: str = "",
                           location: str = "", attendees: str = "") -> dict:
    """STAGED write: create a calendar event. start/end = ISO 8601 WITH timezone
    (e.g. '2026-08-11T09:00:00+08:00'). attendees = comma-separated emails.
    Returns a preview + operation_id; apply with google_write_commit."""
    c = _svc("calendar", "v3")

    def apply():
        body = {"summary": summary, "start": {"dateTime": start}, "end": {"dateTime": end}}
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": a.strip()} for a in attendees.split(",") if a.strip()]
        return c.events().insert(calendarId="primary", body=body).execute()

    return _stage("google_calendar_create",
                  {"summary": summary, "start": start, "end": end, "attendees": attendees},
                  apply,
                  ["start/end provided"],
                  {"summary": summary, "start": start, "end": end,
                   "description": description, "location": location,
                   "attendees": [a.strip() for a in attendees.split(",") if a.strip()]}, None)


@mcp.tool()
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

@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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

@mcp.tool()
def google_tasks_lists() -> list:
    """List the user's task lists. Returns full task list resources (id, title, updated, ...)."""
    t = _svc("tasks", "v1")
    resp = t.tasklists().list(maxResults=100).execute()
    return resp.get("items", [])


@mcp.tool()
def google_tasks_list(tasklist_id: str, max_results: int = 50) -> list:
    """List tasks in a task list. tasklist_id = '@default' for the default list.
    Returns full task resources (id, title, status, due, notes, completed, deleted,
    hidden, position, updated, links, ...)."""
    t = _svc("tasks", "v1")
    resp = t.tasks().list(tasklist=tasklist_id, maxResults=min(max_results, 100)).execute()
    return resp.get("items", [])


@mcp.tool()
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


@mcp.tool()
def google_tasks_update(tasklist_id: str, task_id: str, status: str = "completed") -> dict:
    """STAGED write: mark a task completed (or 'needsAction' to reopen).
    Returns a preview (current vs requested status) + operation_id; apply with
    google_write_commit. Refuses to commit if the task changed since staging."""
    t = _svc("tasks", "v1")
    if status not in {"completed", "needsAction"}:
        raise RuntimeError(f"Invalid status '{status}'; use 'completed' or 'needsAction'.")
    try:
        tk = t.tasks().get(tasklist=tasklist_id, task=task_id).execute()
    except Exception as exc:
        raise RuntimeError(f"Task {task_id} not found: {exc}") from exc

    def apply():
        return t.tasks().update(
            tasklist=tasklist_id, task=task_id, body={"id": task_id, "status": status}
        ).execute()

    def revalidate():
        cur = t.tasks().get(tasklist=tasklist_id, task=task_id).execute()
        if cur.get("status") != tk.get("status"):
            raise RuntimeError("Task status changed since staging; refusing to overwrite. Re-stage.")

    return _stage("google_tasks_update", {"tasklist_id": tasklist_id, "task_id": task_id, "status": status},
                  apply, ["status valid", "task exists"],
                  {"task_id": task_id, "title": tk.get("title"), "current_status": tk.get("status"),
                   "requested_status": status}, revalidate)


@mcp.tool()
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

@mcp.tool()
def google_chat_spaces(max_results: int = 20) -> list:
    """List Google Chat spaces the user belongs to. Returns full space resources
    (name, displayName, spaceType, spaceThreadingState, adminInstalled, ...)."""
    ch = _svc("chat", "v1")
    resp = ch.spaces().list(pageSize=min(max_results, 100)).execute()
    return resp.get("spaces", [])


@mcp.tool()
def google_chat_messages(space_name: str, max_results: int = 20) -> list:
    """List recent messages in a Chat space. space_name like 'spaces/AAAA...'.
    Returns full message resources (text, formattedText, sender, createTime, thread, attachment, ...)."""
    ch = _svc("chat", "v1")
    resp = ch.spaces().messages().list(parent=space_name, pageSize=min(max_results, 100)).execute()
    return resp.get("messages", [])


@mcp.tool()
def google_chat_send(space_name: str, text: str) -> dict:
    """STAGED write: send a message to a Chat space the user is in. space_name like 'spaces/AAAA...'.
    Returns a preview (space + exact message text) + operation_id; apply with google_write_commit."""
    ch = _svc("chat", "v1")

    def apply():
        return ch.spaces().messages().create(parent=space_name, body={"text": text}).execute()

    return _stage("google_chat_send", {"space_name": space_name, "text": text},
                  apply,
                  ["text non-empty"],
                  {"space_name": space_name, "text": text}, None)


# ---------------------------------------------------------------- Meet

@mcp.tool()
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


@mcp.tool()
def google_meet_get_space(space_name: str) -> dict:
    """Get details for a Meet space. space_name like 'spaces/ABC-XYZ123'.
    Returns the full space resource (name, meetingUri, meetingCode, config, activeConference)."""
    mt = _svc("meet", "v2")
    return mt.spaces().get(name=space_name).execute()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
