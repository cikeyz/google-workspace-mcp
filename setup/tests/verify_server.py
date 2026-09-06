#!/usr/bin/env python3
r"""Verification battery for the personal google-workspace MCP server.

Run with the server's own venv python from PowerShell (canonical home):
    $env:HERMES_HOME = "C:\Users\you\AppData\Local\hermes"  # state home until Phase 2
    C:\Users\you\.agents\mcps\google-workspace\.venv\Scripts\python.exe `
      C:\Users\you\.agents\mcps\google-workspace\setup\tests\verify_server.py

Checks:
1. server.py imports cleanly and registers all expected tools (update EXPECTED when adding tools)
2. google_calendar_list live (calendar scope already granted)
3. google_people_contacts live (contacts.readonly scope already granted)
4. google_tasks_lists live (tasks scope granted in the 2026-08-11 re-consent; was a 403 check before that)
5. setup.py --check with HERMES_HOME set -> AUTHENTICATED
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

CANON = Path(__file__).resolve().parent.parent.parent  # setup/tests -> google-workspace
HOME = Path(os.environ.get("GOOGLE_WORKSPACE_HOME",
                           os.environ.get("HERMES_HOME", Path.home() / "AppData/Local/hermes")))
SERVER = CANON / "server.py"
SETUP = CANON / "setup/setup.py"
VENV_PY = CANON / ".venv/Scripts/python.exe"

failures = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))
    if not ok:
        failures.append(name)


# 1. import + tool registration
spec = importlib.util.spec_from_file_location("gw_server", SERVER)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
tool_names = {t.name for t in mod.mcp._tool_manager.list_tools()} if hasattr(mod.mcp, "_tool_manager") else set()
expected = {
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
    "google_gmail_thread_get", "google_gmail_attachment_download",
    "google_people_search", "google_people_get", "google_tasks_get",
    "google_drive_permissions", "google_chat_members", "google_calendar_freebusy",
    "google_sheets_conditional_formats",
    "google_gmail_send", "google_calendar_patch",
    "google_drive_copy", "google_drive_update",
}
missing = expected - tool_names
check(f"tool registration ({len(tool_names)} tools)", not missing, f"missing={sorted(missing)}")
unexpected = tool_names - expected
check("no unlisted tools", not unexpected, f"unexpected={sorted(unexpected)}")

# 2+3. live calls with already-granted scopes
try:
    res = mod.google_calendar_list(max_results=3)
    check("calendar list live", isinstance(res, list), f"{len(res)} events")
except Exception as e:
    check("calendar list live", False, str(e)[:200])

try:
    res = mod.google_people_contacts(max_results=3)
    check("people contacts live", isinstance(res, list), f"{len(res)} contacts")
except Exception as e:
    check("people contacts live", False, str(e)[:200])

# 4. tasks live (re-consent completed 2026-08-11 — 17 scopes granted)
try:
    res = mod.google_tasks_lists()
    check("tasks lists live", isinstance(res, list), f"{len(res)} lists")
except Exception as e:
    check("tasks lists live", False, str(e)[:200])

# 5. setup.py --check with correct state home (neutral var only: proves
# HERMES_HOME is no longer needed anywhere in the chain)
env = dict(os.environ)
env["GOOGLE_WORKSPACE_HOME"] = str(HOME)
env.pop("HERMES_HOME", None)
env.pop("PYTHONPATH", None)
proc = subprocess.run([str(VENV_PY), str(SETUP), "--check"], capture_output=True, text=True, env=env)
out = (proc.stdout + proc.stderr).strip()
ok = proc.returncode == 0 and "AUTHENTICATED" in out
check("setup.py --check", ok, out.splitlines()[-1][:160])

print()
if failures:
    print(f"RESULT: {len(failures)} FAILED -> {failures}")
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")
