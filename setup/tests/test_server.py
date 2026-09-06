#!/usr/bin/env python3
r"""Full test battery for the personal google-workspace MCP server.

Run with the server's own venv python from PowerShell (canonical home):
    $env:GOOGLE_WORKSPACE_HOME = "C:\Users\YOU\.google-workspace-mcp"  # or wherever your state/ lives
    C:\Users\YOU\.agents\mcps\google-workspace\.venv\Scripts\python.exe `
      C:\Users\YOU\.agents\mcps\google-workspace\setup\tests\test_server.py

Coverage:
1. Tool registration (41 tools)
2. Live READ checks across every service (real known IDs; read-only)
3. Staged-write E2E per write tool with SELF-CLEANUP:
   calendar create->delete, tasks create->delete, drive folder create->trash,
   docs create->trash, sheets create->trash, drive_share on a test folder->trash.
   chat_send and meet_create_space are STAGE-ONLY (chat messages are visible to
   people; Meet spaces have no delete API) -> cancel, never commit.
4. Safety semantics: double-commit refused, cancel discards, cron commit fails
   closed (HERMES_CRON_SESSION), TTL purge, staging cap.
5. Audit file grows after commits.

All created artifacts use the GW-TEST- prefix and are cleaned in the same run;
a pre-clean pass trashes leftovers from previously failed runs.
"""
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CANON = Path(__file__).resolve().parent.parent.parent  # setup/tests -> google-workspace-mcp
HOME = Path(os.environ.get("GOOGLE_WORKSPACE_HOME",
                           os.environ.get("HERMES_HOME", Path.home() / ".google-workspace-mcp")))
SERVER = CANON / "server.py"
AUDIT = HOME / "logs/google-write-audit.jsonl"

# Test fixtures via env (set GW_FIXTURE_DOC_ID / GW_FIXTURE_FORM_ID to your own
# readable Doc / Form; fixture-dependent checks SKIP when unset).
FORM_ID = os.environ.get("GW_FIXTURE_FORM_ID", "")
FORM_RANGE = os.environ.get("GW_FIXTURE_RANGE", "A1:B2")
DOC_ID = os.environ.get("GW_FIXTURE_DOC_ID", "")
PREFIX = "GW-TEST-"


def skipped(name, why):
    print(f"[SKIP] {name} -- {why}")

spec = importlib.util.spec_from_file_location("gw_server", SERVER)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

failures = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def commit_ok(name, staged):
    r = mod.google_write_commit(staged["operation_id"])
    check(name, r.get("applied") is True, f"op={staged['operation_id']}")
    return r.get("result", {})


def audit_lines_before():
    return len(AUDIT.read_text(encoding="utf-8").splitlines()) if AUDIT.exists() else 0


# ---------------------------------------------------------------- 1. registration
tool_names = {t.name for t in mod.mcp._tool_manager.list_tools()} if hasattr(mod.mcp, "_tool_manager") else set()
EXPECTED = {
    "google_auth_status",
    "google_write_commit", "google_write_cancel", "google_write_list_staged",
    "google_sheets_metadata", "google_sheets_read", "google_sheets_update",
    "google_sheets_append", "google_sheets_create",
    "google_drive_search", "google_drive_get", "google_drive_download",
    "google_drive_upload", "google_drive_create_folder", "google_drive_share",
    "google_drive_trash",
    "google_docs_read", "google_docs_create", "google_docs_append",
    "google_forms_list", "google_forms_get", "google_forms_responses",
    "google_gmail_search", "google_gmail_get",
    "google_calendar_list", "google_calendar_get", "google_calendar_create",
    "google_calendar_delete",
    "google_people_contacts",
    "google_slides_get", "google_slides_create",
    "google_tasks_lists", "google_tasks_list", "google_tasks_create",
    "google_tasks_update", "google_tasks_delete",
    "google_chat_spaces", "google_chat_messages", "google_chat_send",
    "google_meet_create_space", "google_meet_get_space",
}
missing = EXPECTED - tool_names
check(f"tool registration ({len(tool_names)} tools)", not missing, f"missing={sorted(missing)}")

# ---------------------------------------------------------------- 2. live reads
sheet_id = None
if not FORM_ID:
    skipped("forms_get full payload", "no GW_FIXTURE_FORM_ID")
    skipped("sheets_metadata/read live", "no GW_FIXTURE_FORM_ID")
    skipped("drive_get full payload (form)", "no GW_FIXTURE_FORM_ID")
else:
    try:
        form = mod.google_forms_get(FORM_ID)
        sheet_id = form.get("linkedSheetId")
        check("forms_get full payload", all(k in form for k in ("items", "settings", "responderUri")),
              f"{len(form.get('items', []))} items")
    except Exception as e:
        check("forms_get full payload", False, str(e)[:160])

if sheet_id:
    try:
        meta = mod.google_sheets_metadata(sheet_id)
        check("sheets_metadata live", "sheets" in meta, f"{len(meta.get('sheets', []))} tabs")
        vals = mod.google_sheets_read(sheet_id, FORM_RANGE)
        check("sheets_read live", "values" in vals and "range" in vals, f"{len(vals.get('values', []))} rows")
    except Exception as e:
        check("sheets_metadata/read live", False, str(e)[:160])
elif FORM_ID:
    check("sheets_metadata/read live", False, "no linkedSheetId")

if FORM_ID:
    try:
        dg = mod.google_drive_get(FORM_ID)
        check("drive_get full payload", "owners" in dg and "capabilities" in dg, dg.get("name", ""))
    except Exception as e:
        check("drive_get full payload", False, str(e)[:160])

if not DOC_ID:
    skipped("docs_read live", "no GW_FIXTURE_DOC_ID")
else:
    try:
        doc = mod.google_docs_read(DOC_ID)
        check("docs_read live", bool(doc.get("title")), doc.get("title", ""))
    except Exception as e:
        check("docs_read live", False, str(e)[:160])

try:
    msgs = mod.google_gmail_search("newer_than:90d", 3)
    check("gmail_search live", isinstance(msgs, list), f"{len(msgs)} msgs")
except Exception as e:
    check("gmail_search live", False, str(e)[:160])

try:
    evs = mod.google_calendar_list(max_results=5)
    check("calendar_list live", isinstance(evs, list), f"{len(evs)} events")
except Exception as e:
    check("calendar_list live", False, str(e)[:160])

try:
    tls = mod.google_tasks_lists()
    check("tasks_lists live", isinstance(tls, list), f"{len(tls)} lists")
except Exception as e:
    check("tasks_lists live", False, str(e)[:160])

try:
    spaces = mod.google_chat_spaces(5)
    check("chat_spaces live", isinstance(spaces, list), f"{len(spaces)} spaces")
except Exception as e:
    check("chat_spaces live", False, str(e)[:160])

# ---------------------------------------------------------------- 3. pre-clean leftovers
try:
    leftovers = mod.google_drive_search(f"name contains '{PREFIX}'", 50)
    for f in leftovers:
        if not f.get("trashed"):
            mod.google_write_commit(mod.google_drive_trash(f["id"])["operation_id"])
    check("pre-clean GW-TEST leftovers", True, f"trashed {len(leftovers)} leftover(s)")
except Exception as e:
    check("pre-clean GW-TEST leftovers", False, str(e)[:160])

# ---------------------------------------------------------------- 4. E2E write cycles
stamp = str(int(time.time()))
audit_before = audit_lines_before()

# 4a. Calendar: create -> commit -> verify -> delete -> commit -> verify
try:
    st = mod.google_calendar_create(f"{PREFIX}EVT-{stamp}",
                                    (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                                    (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat())
    check("calendar create staged (nothing applied)",
          not [e for e in mod.google_calendar_list(max_results=50) if e.get("summary", "").startswith(PREFIX)])
    ev = commit_ok("calendar create committed", st)
    check("calendar create verified live", bool(
        [e for e in mod.google_calendar_list(max_results=50) if e.get("id") == ev.get("id")]))
    check("calendar delete previews target", mod.google_calendar_delete(ev["id"])["preview"].get("summary", "").startswith(PREFIX))
    commit_ok("calendar delete committed", mod.google_calendar_delete(ev["id"]))
    check("calendar delete verified gone", not [
        e for e in mod.google_calendar_list(max_results=50) if e.get("id") == ev.get("id")])
except Exception as e:
    check("calendar E2E cycle", False, str(e)[:200])

# 4b. Tasks: create -> commit -> verify -> delete -> commit -> verify
try:
    st = mod.google_tasks_create("@default", f"{PREFIX}TASK-{stamp}")
    tk = commit_ok("tasks create committed", st)
    found = [x for x in mod.google_tasks_list("@default", 100) if x.get("id") == tk.get("id")]
    check("tasks create verified live", bool(found))
    commit_ok("tasks delete committed", mod.google_tasks_delete("@default", tk["id"]))
    check("tasks delete verified gone", not [
        x for x in mod.google_tasks_list("@default", 100) if x.get("id") == tk.get("id")])
except Exception as e:
    check("tasks E2E cycle", False, str(e)[:200])

# 4c. Drive folder: create -> commit -> verify -> trash -> commit -> verify
try:
    st = mod.google_drive_create_folder(f"{PREFIX}FOLDER-{stamp}")
    folder = commit_ok("drive folder create committed", st)
    check("drive folder verified live", mod.google_drive_get(folder["id"]).get("name", "").startswith(PREFIX))
    commit_ok("drive folder trash committed", mod.google_drive_trash(folder["id"]))
    check("drive folder trashed verified", mod.google_drive_get(folder["id"]).get("trashed") is True)
except Exception as e:
    check("drive folder E2E cycle", False, str(e)[:200])

# 4d. Docs: create -> commit -> verify -> trash -> commit
try:
    st = mod.google_docs_create(f"{PREFIX}DOC-{stamp}")
    doc = commit_ok("docs create committed", st)
    check("docs create verified live", mod.google_drive_get(doc["documentId"]).get("name", "").startswith(PREFIX))
    commit_ok("docs trash committed", mod.google_drive_trash(doc["documentId"]))
except Exception as e:
    check("docs E2E cycle", False, str(e)[:200])

# 4e. Sheets: create -> commit -> verify -> trash -> commit
try:
    st = mod.google_sheets_create(f"{PREFIX}SHEET-{stamp}")
    ss = commit_ok("sheets create committed", st)
    check("sheets create verified live", mod.google_drive_get(ss["spreadsheetId"]).get("name", "").startswith(PREFIX))
    commit_ok("sheets trash committed", mod.google_drive_trash(ss["spreadsheetId"]))
except Exception as e:
    check("sheets E2E cycle", False, str(e)[:200])

# 4f. Drive share: on the test folder only -> commit -> verify permission -> trash folder
try:
    st = mod.google_drive_create_folder(f"{PREFIX}SHARE-{stamp}")
    folder = commit_ok("share-target folder create committed", st)
    sh = mod.google_drive_share(folder["id"], email="", role="reader")
    perm = commit_ok("drive_share committed", sh)
    check("drive_share permission verified", perm.get("id") is not None)
    commit_ok("share-target folder trash committed", mod.google_drive_trash(folder["id"]))
except Exception as e:
    check("drive_share E2E cycle", False, str(e)[:200])

# 4g. Stage-only tools (never commit): chat_send (visible to people), meet (no delete API)
try:
    st = mod.google_chat_send(spaces[0]["name"] if spaces else "spaces/AAAA", f"{PREFIX}chat-{stamp}")
    mod.google_write_cancel(st["operation_id"])
    check("chat_send stage-only + cancel", True, "never committed")
except Exception as e:
    check("chat_send stage-only + cancel", False, str(e)[:160])
try:
    st = mod.google_meet_create_space()
    mod.google_write_cancel(st["operation_id"])
    check("meet stage-only + cancel", True, "never committed")
except Exception as e:
    check("meet stage-only + cancel", False, str(e)[:160])

# ---------------------------------------------------------------- 5. safety semantics
# double-commit refused
try:
    st = mod.google_calendar_create(f"{PREFIX}DUP-{stamp}", "2026-12-01T00:00:00Z", "2026-12-01T01:00:00Z")
    mod.google_write_commit(st["operation_id"])
    mod.google_write_commit(st["operation_id"])
    check("double-commit refused", False, "second commit succeeded (bug)")
except Exception as e:
    check("double-commit refused", "Unknown, cancelled, or already-applied" in str(e))

# cron fail-closed
os.environ["HERMES_CRON_SESSION"] = "1"
try:
    st = mod.google_calendar_create(f"{PREFIX}CRON-{stamp}", "2026-12-01T00:00:00Z", "2026-12-01T01:00:00Z")
    mod.google_write_commit(st["operation_id"])
    check("cron commit fails closed", False, "commit succeeded in cron (bug)")
except Exception as e:
    check("cron commit fails closed", "cron" in str(e).lower())
finally:
    del os.environ["HERMES_CRON_SESSION"]

# TTL purge (white-box: inject an expired op)
try:
    mod._STAGED["expired1"] = {"tool": "t", "created_ts": time.time() - 999999}
    mod.google_write_list_staged()
    check("TTL purge drops expired ops", "expired1" not in mod._STAGED)
except Exception as e:
    check("TTL purge drops expired ops", False, str(e)[:160])

# staging cap (white-box: fill to the cap)
try:
    for i in range(20):
        mod._STAGED[f"cap{i}"] = {"tool": "t", "created_ts": time.time()}
    mod.google_calendar_create(f"{PREFIX}CAP-{stamp}", "2026-12-01T00:00:00Z", "2026-12-01T01:00:00Z")
    check("staging cap enforced", False, "stage succeeded over cap (bug)")
except Exception as e:
    check("staging cap enforced", "Too many staged operations" in str(e))
finally:
    for k in [k for k in mod._STAGED if k.startswith("cap")]:
        mod._STAGED.pop(k, None)

# ---------------------------------------------------------------- 6. audit growth
audit_after = audit_lines_before()
check("audit file grew with commits", audit_before < audit_after,
      f"{audit_before} -> {audit_after}")

print()
if failures:
    print(f"RESULT: {len(failures)} FAILED -> {failures}")
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")
