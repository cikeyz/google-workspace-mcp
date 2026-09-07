#!/usr/bin/env python3
r"""Full test battery for the personal google-workspace MCP server.

Run with the server's own venv python from PowerShell (canonical home):
    $env:HERMES_HOME = "C:\Users\YOU\AppData\Local\hermes"  # state home until Phase 2
    C:\Users\YOU\.agents\mcps\google-workspace\.venv\Scripts\python.exe `
      C:\Users\YOU\.agents\mcps\google-workspace\setup\tests\test_server.py

Coverage:
1. Tool registration (73 tools, 4 prompts)
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

CANON = Path(__file__).resolve().parent.parent.parent  # setup/tests -> google-workspace
HOME = Path(os.environ.get("GOOGLE_WORKSPACE_HOME",
                           os.environ.get("HERMES_HOME", Path.home() / "AppData/Local/hermes")))
SERVER = CANON / "server.py"
AUDIT = HOME / "logs/google-write-audit.jsonl"

# Personal fixtures (env overrides; company IDs are dead on the personal account).
# DOC_ID default CPM-A1 was verified readable on you@gmail.com 2026-09-06.
FORM_ID = os.environ.get("GW_FIXTURE_FORM_ID", "")
FORM_RANGE = os.environ.get("GW_FIXTURE_RANGE", "A1:B2")
DOC_ID = os.environ.get("GW_FIXTURE_DOC_ID", "YOUR_DOC_ID")
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
    "google_gmail_thread_get", "google_gmail_attachment_download",
    "google_people_search", "google_people_get", "google_tasks_get",
    "google_drive_permissions", "google_chat_members", "google_calendar_freebusy",
    "google_sheets_conditional_formats",
    "google_gmail_send", "google_calendar_patch",
    "google_drive_copy", "google_drive_update",
    "google_gmail_labels_list", "google_gmail_modify_labels",
    "google_gmail_search_threads",
    "google_drive_recent", "google_drive_read_content", "google_drive_create_file",
    "google_sheets_insert_dimension",
    "google_calendar_list_calendars", "google_calendar_search_events",
    "google_calendar_respond", "google_calendar_suggest_time",
    "google_chat_search_conversations", "google_chat_mark_read",
    "google_chat_mark_unread",
    "google_people_profile", "google_people_search_contacts",
    "google_universal_search",
    "google_docs_update", "google_slides_update",
}
missing = EXPECTED - tool_names
check(f"tool registration ({len(tool_names)} tools)", not missing, f"missing={sorted(missing)}")
unexpected = tool_names - EXPECTED
check("no unlisted tools", not unexpected, f"unexpected={sorted(unexpected)}")

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

try:
    doc = mod.google_docs_read(DOC_ID)
    check("docs_read live", bool(doc.get("title")), doc.get("title", ""))
except Exception as e:
    check("docs_read live", False, str(e)[:160])

try:
    res = mod.google_gmail_search("newer_than:90d", 3)
    msgs = res["items"]
    check("gmail_search live", isinstance(msgs, list), f"{len(msgs)} msgs")
    check("gmail_search envelope", {"items", "next_page_token", "has_more"} <= set(res), "")
except Exception as e:
    check("gmail_search live", False, str(e)[:160])

try:
    res = mod.google_calendar_list(max_results=5)
    evs = res["items"]
    check("calendar_list live", isinstance(evs, list), f"{len(evs)} events")
    check("calendar_list envelope", {"items", "next_page_token", "has_more"} <= set(res), "")
except Exception as e:
    check("calendar_list live", False, str(e)[:160])

try:
    res = mod.google_tasks_lists()
    tls = res["items"]
    check("tasks_lists live", isinstance(tls, list), f"{len(tls)} lists")
except Exception as e:
    check("tasks_lists live", False, str(e)[:160])

try:
    res = mod.google_chat_spaces(5)
    spaces = res["items"]
    check("chat_spaces live", isinstance(spaces, list), f"{len(spaces)} spaces")
except Exception as e:
    check("chat_spaces live", False, str(e)[:160])
    spaces = []

# ---------------------------------------------------------------- 3. pre-clean leftovers
try:
    leftovers = mod.google_drive_search(f"name contains '{PREFIX}'", 50)["items"]
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
          not [e for e in mod.google_calendar_list(max_results=50)["items"] if e.get("summary", "").startswith(PREFIX)])
    ev = commit_ok("calendar create committed", st)
    check("calendar create verified live", bool(
        [e for e in mod.google_calendar_list(max_results=50)["items"] if e.get("id") == ev.get("id")]))
    check("calendar delete previews target", mod.google_calendar_delete(ev["id"])["preview"].get("summary", "").startswith(PREFIX))
    commit_ok("calendar delete committed", mod.google_calendar_delete(ev["id"]))
    check("calendar delete verified gone", not [
        e for e in mod.google_calendar_list(max_results=50)["items"] if e.get("id") == ev.get("id")])
except Exception as e:
    check("calendar E2E cycle", False, str(e)[:200])

# 4b. Tasks: create -> commit -> verify -> delete -> commit -> verify
try:
    st = mod.google_tasks_create("@default", f"{PREFIX}TASK-{stamp}")
    tk = commit_ok("tasks create committed", st)
    found = [x for x in mod.google_tasks_list("@default", 100)["items"] if x.get("id") == tk.get("id")]
    check("tasks create verified live", bool(found))
    commit_ok("tasks delete committed", mod.google_tasks_delete("@default", tk["id"]))
    check("tasks delete verified gone", not [
        x for x in mod.google_tasks_list("@default", 100)["items"] if x.get("id") == tk.get("id")])
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

# 4g. Stage-only tools (never commit): chat_send (visible to people), meet (no delete API),
# gmail_send (sent mail cannot be un-sent)
try:
    spaces = mod.google_chat_spaces(5)["items"]
except Exception:
    spaces = []
try:
    if not spaces:
        skipped("chat_send stage-only + cancel", "no chat spaces visible")
    else:
        st = mod.google_chat_send(spaces[0]["name"], f"{PREFIX}chat-{stamp}")
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
try:
    st = mod.google_gmail_send("placeholder@localhost", f"{PREFIX}SUBJ-{stamp}", "probe body")
    mod.google_write_cancel(st["operation_id"])
    check("gmail_send stage-only + cancel", True, "never committed")
except Exception as e:
    check("gmail_send stage-only + cancel", False, str(e)[:160])

# 4h. tasks_update PATCH cycle (title+status, no wipe): create -> patch -> get-verify -> delete
try:
    st = mod.google_tasks_create("@default", f"{PREFIX}PATCH-{stamp}")
    tk = commit_ok("patch-target task created", st)
    st = mod.google_tasks_update("@default", tk["id"], "completed", title=f"{PREFIX}PATCHED-{stamp}")
    commit_ok("tasks_update patch committed", st)
    got = mod.google_tasks_get("@default", tk["id"])
    check("tasks_update preserved title", got.get("title") == f"{PREFIX}PATCHED-{stamp}", got.get("status", ""))
    commit_ok("patch-target task deleted", mod.google_tasks_delete("@default", tk["id"]))
except Exception as e:
    check("tasks_update PATCH cycle", False, str(e)[:200])

# 4i. calendar_patch cycle: create -> patch description -> get-verify -> delete
try:
    st = mod.google_calendar_create(f"{PREFIX}PATCH-{stamp}", "2026-12-02T00:00:00Z", "2026-12-02T01:00:00Z")
    ev = commit_ok("patch-target event created", st)
    st = mod.google_calendar_patch(ev["id"], description=f"{PREFIX}desc-{stamp}")
    commit_ok("calendar_patch committed", st)
    check("calendar_patch verified", f"{PREFIX}desc-{stamp}" in
          mod.google_calendar_get(ev["id"]).get("description", ""))
    commit_ok("patch-target event deleted", mod.google_calendar_delete(ev["id"]))
except Exception as e:
    check("calendar_patch cycle", False, str(e)[:200])

# 4j. drive_copy + drive_update cycle: doc -> copy -> rename copy -> trash both
# (Drive folders cannot be copied via files.copy, so the subject is a doc)
try:
    st = mod.google_docs_create(f"{PREFIX}COPYSRC-{stamp}")
    src = commit_ok("copy-source doc created", st)
    cp = commit_ok("drive_copy committed",
                   mod.google_drive_copy(src["documentId"], name=f"{PREFIX}COPY2-{stamp}"))
    check("drive_copy verified", cp.get("name", "").startswith(PREFIX))
    commit_ok("drive_update rename committed",
              mod.google_drive_update(cp["id"], name=f"{PREFIX}COPY3-{stamp}"))
    check("drive_update verified",
          mod.google_drive_get(cp["id"]).get("name", "").startswith(PREFIX))
    commit_ok("copy trash committed", mod.google_drive_trash(cp["id"]))
    commit_ok("source trash committed", mod.google_drive_trash(src["documentId"]))
except Exception as e:
    check("drive_copy/update cycle", False, str(e)[:200])

# 4k. new-read live checks
try:
    st = mod.google_tasks_create("@default", f"{PREFIX}GET-{stamp}")
    gk = commit_ok("get-target task created", st)
    check("tasks_get live", mod.google_tasks_get("@default", gk["id"]).get("title", "").startswith(PREFIX), "")
    commit_ok("get-target task deleted", mod.google_tasks_delete("@default", gk["id"]))
except Exception as e:
    check("tasks_get live", False, str(e)[:160])
try:
    perms = mod.google_drive_permissions(folder["id"])
    check("drive_permissions live", isinstance(perms.get("permissions"), list),
          f"{len(perms.get('permissions', []))} perms")
except Exception as e:
    check("drive_permissions live", False, str(e)[:160])
try:
    if spaces:
        res = mod.google_chat_members(spaces[0]["name"], 5)
        check("chat_members live", isinstance(res.get("items"), list), f"{res.get('result_count', 0)} members")
    else:
        skipped("chat_members live", "no chat spaces visible")
except Exception as e:
    check("chat_members live", False, str(e)[:160])
try:
    fb = mod.google_calendar_freebusy("2026-12-01T00:00:00Z", "2026-12-02T00:00:00Z")
    check("calendar_freebusy live", "calendars" in fb, "")
except Exception as e:
    check("calendar_freebusy live", False, str(e)[:160])
try:
    res = mod.google_gmail_search("newer_than:1d", 1)["items"]
    if res:
        th = mod.google_gmail_thread_get(res[0].get("threadId", ""))
        check("gmail_thread_get live", bool(th.get("messages")), f"{len(th.get('messages', []))} msgs")
    else:
        skipped("gmail_thread_get live", "inbox empty for newer_than:1d")
except Exception as e:
    check("gmail_thread_get live", False, str(e)[:160])
try:
    found = mod.google_people_search("a", 3)["items"]
    check("people_search live", isinstance(found, list), f"{len(found)} hits")
except Exception as e:
    check("people_search live", False, str(e)[:160])

# 4l2. v2.3 live reads backfill (all read-only, no cleanup)
try:
    labels = mod.google_gmail_labels_list()["items"]
    check("gmail_labels_list live", any(l.get("id") == "INBOX" for l in labels),
          f"{len(labels)} labels")
except Exception as e:
    check("gmail_labels_list live", False, str(e)[:160])
try:
    th = mod.google_gmail_search_threads("newer_than:90d", max_results=2)
    check("gmail_search_threads live", isinstance(th.get("items"), list),
          f"{th.get('result_count', 0)} threads")
except Exception as e:
    check("gmail_search_threads live", False, str(e)[:160])
try:
    rec = mod.google_drive_recent(max_results=2)
    check("drive_recent live", isinstance(rec.get("items"), list),
          f"{rec.get('result_count', 0)} files")
except Exception as e:
    check("drive_recent live", False, str(e)[:160])
try:
    cals = mod.google_calendar_list_calendars(max_results=10)["items"]
    check("list_calendars live", any(c.get("primary") for c in cals),
          f"{len(cals)} calendars")
except Exception as e:
    check("list_calendars live", False, str(e)[:160])
try:
    sug = mod.google_calendar_suggest_time("2026-12-10T00:00:00Z", "2026-12-10T12:00:00Z", 60)
    check("suggest_time live", isinstance(sug.get("slots"), list),
          f"{sug.get('slot_count', 0)} slots")
except Exception as e:
    check("suggest_time live", False, str(e)[:160])
try:
    conv = mod.google_chat_search_conversations("a", 5)
    check("search_conversations live", isinstance(conv.get("items"), list),
          f"{conv.get('result_count', 0)} spaces")
except Exception as e:
    check("search_conversations live", False, str(e)[:160])
try:
    prof = mod.google_people_profile()
    check("people_profile live", bool((prof.get("names") or [{}])[0].get("displayName")), "")
except Exception as e:
    check("people_profile live", False, str(e)[:160])
try:
    sc = mod.google_people_search_contacts("a", 3)
    check("search_contacts live", isinstance(sc.get("items"), list),
          f"{sc.get('result_count', 0)} hits")
except Exception as e:
    check("search_contacts live", False, str(e)[:160])
try:
    uni = mod.google_universal_search(f"GW-TEST-NOPE-{stamp}", ["drive", "gmail"], 2)
    check("universal_search live", uni.get("result_count", -1) >= 0,
          f"queried={uni.get('sources_queried')}")
except Exception as e:
    check("universal_search live", False, str(e)[:160])

# 4l. cursor round-trip on calendar (seeded events) + empty-page contract
try:
    seeds = []
    for i in range(3):
        st = mod.google_calendar_create(f"{PREFIX}PAGE-{stamp}-{i}", "2026-12-03T00:00:00Z", "2026-12-03T01:00:00Z")
        seeds.append(commit_ok(f"page-seed {i} created", st)["id"])
    p1 = mod.google_calendar_list("2026-12-03T00:00:00Z", "2026-12-04T00:00:00Z", max_results=2)
    check("cursor page 1", len(p1["items"]) == 2 and p1["has_more"], "")
    p2 = mod.google_calendar_list("2026-12-03T00:00:00Z", "2026-12-04T00:00:00Z",
                                  max_results=2, page_token=p1["next_page_token"])
    ids1 = {e["id"] for e in p1["items"]}
    ids2 = {e["id"] for e in p2["items"]}
    check("cursor page 2 disjoint", bool(ids2) and not (ids1 & ids2), "")
    full = mod.google_calendar_list("2026-12-03T00:00:00Z", "2026-12-04T00:00:00Z", max_results=50)
    check("cursor union subset", (ids1 | ids2) <= {e["id"] for e in full["items"]}, "")
    for sid in seeds:
        commit_ok(f"page-seed {sid[:8]} deleted", mod.google_calendar_delete(sid))
except Exception as e:
    check("cursor round-trip", False, str(e)[:200])
try:
    empty = mod.google_drive_search(f"name contains 'GW-TEST-NOPE-{stamp}'", 10)
    check("empty page contract", empty["items"] == [] and empty["has_more"] is False, "")
except Exception as e:
    check("empty page contract", False, str(e)[:160])

# 4m. gmail modify STAR restore cycle (add, verify, remove-if-absent-before, verify)
try:
    newest = mod.google_gmail_search("newer_than:90d", 1)["items"]
    if not newest:
        skipped("gmail_modify live", "inbox empty for newer_than:90d")
    else:
        mid = newest[0]["id"]
        before = mod.google_gmail_get(mid).get("labelIds", [])
        had_star = "STARRED" in before
        commit_ok("gmail star added",
                  mod.google_gmail_modify_labels(mid, add_label_ids="STARRED"))
        check("gmail star verified",
              "STARRED" in mod.google_gmail_get(mid).get("labelIds", []), "")
        if not had_star:
            commit_ok("gmail star removed",
                      mod.google_gmail_modify_labels(mid, remove_label_ids="STARRED"))
            check("gmail star restored",
                  "STARRED" not in mod.google_gmail_get(mid).get("labelIds", []), "")
except Exception as e:
    check("gmail_modify live", False, str(e)[:200])

# 4n. drive create_file + read_content + trash cycle
try:
    st = mod.google_drive_create_file(f"{PREFIX}FILE-{stamp}", "v24 battery probe text")
    created = commit_ok("drive_create_file committed", st)
    fid = created["id"]
    check("drive_create verified", created.get("name", "").startswith(PREFIX), "")
    body = mod.google_drive_read_content(fid)
    check("drive_read_content verified", "v24 battery probe" in body.get("text", ""), "")
    commit_ok("drive_create residue trashed", mod.google_drive_trash(fid))
except Exception as e:
    check("drive create/read cycle", False, str(e)[:200])

# 4o. sheets insert_dimension cycle (row count N -> N+1, whole-sheet trash)
try:
    st = mod.google_sheets_create(f"{PREFIX}DIM-{stamp}")
    ss = commit_ok("dim-seed sheet created", st)
    sid = ss["spreadsheetId"]
    meta0 = mod.google_sheets_metadata(sid)
    tab = meta0["sheets"][0]["properties"]
    n0 = tab["gridProperties"]["rowCount"]
    commit_ok("insert_dimension committed",
              mod.google_sheets_insert_dimension(sid, tab["sheetId"], "ROWS", 1, 2))
    n1 = mod.google_sheets_metadata(sid)["sheets"][0]["properties"]["gridProperties"]["rowCount"]
    check("insert_dimension verified", n1 == n0 + 1, f"{n0}->{n1}")
    found = mod.google_drive_search(f"name contains '{PREFIX}DIM-{stamp}'", 5)["items"]
    for f in found:
        commit_ok(f"dim-seed {f['id'][:8]} trashed", mod.google_drive_trash(f["id"]))
except Exception as e:
    check("sheets insert_dimension cycle", False, str(e)[:200])

# 4p. calendar respond refusal paths on a seeded event (commit path needs an
# event where the user is a guest, not creatable via the API as organizer)
try:
    prof = mod.google_people_profile()
    self_email = (prof.get("emailAddresses") or [{}])[0].get("value", "")
    st = mod.google_calendar_create(f"{PREFIX}RSVP-{stamp}", "2026-12-04T00:00:00Z",
                                    "2026-12-04T01:00:00Z", attendees=self_email)
    ev = commit_ok("rsvp-seed event created", st)
    evid = ev["id"]
    try:
        mod.google_calendar_respond(evid, "bogus")
        check("respond bad-value refused", False, "accepted bogus response (bug)")
    except Exception as e2:
        check("respond bad-value refused", "accepted/tentative/declined" in str(e2), "")
    try:
        mod.google_calendar_respond(evid, "tentative")
        check("respond non-attendee refused", False, "organizer RSVP applied (bug)")
    except Exception as e2:
        check("respond non-attendee refused", "not an attendee" in str(e2), str(e2)[:120])
    commit_ok("patch attendees+visibility committed",
              mod.google_calendar_patch(evid, attendees=self_email, visibility="private"))
    got2 = mod.google_calendar_get(evid)
    check("patch extension verified", got2.get("visibility") == "private", "")
    commit_ok("rsvp-seed deleted", mod.google_calendar_delete(evid))
except Exception as e:
    check("calendar respond cycle", False, str(e)[:200])

# 4q. chat mark_read -> mark_unread paired commits (ends unread = badge restored).
# Commits need a Chat app configured in the project; without it the API 404s.
try:
    if spaces:
        try:
            commit_ok("mark_read committed", mod.google_chat_mark_read(spaces[0]["name"]))
            commit_ok("mark_unread committed", mod.google_chat_mark_unread(spaces[0]["name"]))
            check("chat read-state pair live", True, "")
        except Exception as e2:
            if "not found" in str(e2).lower() and "chat app" in str(e2).lower():
                skipped("chat read-state pair live", "no Chat app configured in project")
            else:
                raise
    else:
        skipped("chat read-state pair live", "no chat spaces visible")
except Exception as e:
    check("chat read-state pair live", False, str(e)[:200])

# 4r. docs_update + slides_update E2E (stage, commit, verify content, trash seeds)
try:
    st = mod.google_docs_create(f"{PREFIX}DOCUPD-{stamp}", body="Alpha line.\nBeta line.\n")
    doc = commit_ok("docupd-seed created", st)
    doc_id = doc["documentId"]
    st = mod.google_docs_update(doc_id, [{"insertText": {"location": {"index": 1},
                                                         "text": "TOP. "}}])
    commit_ok("docs_update committed", st)
    check("docs_update verified", "TOP." in mod.google_docs_read(doc_id)["text"], "")
    commit_ok("docupd-seed trashed", mod.google_drive_trash(doc_id))
except Exception as e:
    check("docs_update cycle", False, str(e)[:200])
try:
    st = mod.google_slides_create(f"{PREFIX}SLDUPD-{stamp}")
    deck = commit_ok("sldupd-seed created", st)
    pid = deck["presentationId"]
    commit_ok("slides_update committed",
              mod.google_slides_update(pid, [{"createSlide": {"insertionIndex": 1}}]))
    check("slides_update verified", mod.google_slides_get(pid)["slide_count"] == 2, "")
    found = mod.google_drive_search(f"name contains '{PREFIX}SLDUPD-{stamp}'", 5)["items"]
    for f in found:
        commit_ok(f"sldupd-seed {f['id'][:8]} trashed", mod.google_drive_trash(f["id"]))
except Exception as e:
    check("slides_update cycle", False, str(e)[:200])

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

# ---------------------------------------------------------------- 6. v2.1 guardrails
# formula tripwire (no writes)
try:
    mod.google_sheets_update("x", "A1:A1", [["=IMPORTXML(1,2)"]],
                             value_input_option="USER_ENTERED")
    check("formula tripwire blocks", False, "USER_ENTERED formula write staged (bug)")
except Exception as e:
    check("formula tripwire blocks", "BLOCKED" in str(e) and "allow_formulas" in str(e))
# OVERWRITE ack gate (no writes)
try:
    mod.google_sheets_append("x", "A1:A1", [["v"]], insert_data_option="OVERWRITE")
    check("overwrite ack gate", False, "OVERWRITE staged without ack (bug)")
except Exception as e:
    check("overwrite ack gate", "overwrite_acknowledged" in str(e))
# thread_key + meet config + locale ride the stage path (cancelled, never committed)
try:
    st = mod.google_chat_send(spaces[0]["name"] if spaces else "spaces/AAAA", "probe",
                              thread_key="thread-123_ABC")
    check("thread_key preview", st["preview"].get("thread_key") == "thread-123_ABC", "")
    mod.google_write_cancel(st["operation_id"])
    try:
        mod.google_chat_send("spaces/AAAA", "probe", thread_key="bad key!")
        check("thread_key validation", False, "bad key staged (bug)")
    except Exception as e2:
        check("thread_key validation", "thread_key" in str(e2), "")
except Exception as e:
    check("thread_key preview", False, str(e)[:160])
try:
    st = mod.google_meet_create_space({"accessType": "OPEN"})
    mod.google_write_cancel(st["operation_id"])
    check("meet OPEN refused", False, "OPEN staged (bug)")
except Exception as e:
    check("meet OPEN refused", "OPEN" in str(e) or "TRUSTED" in str(e))
try:
    st = mod.google_sheets_create(f"{PREFIX}LOCALE-{stamp}", locale="en_US", time_zone="America/New_York")
    ss = commit_ok("locale sheet created", st)
    check("locale sheet verified",
          mod.google_sheets_metadata(ss["spreadsheetId"]).get("properties", {}).get("locale") == "en_US", "")
    commit_ok("locale sheet trashed", mod.google_drive_trash(ss["spreadsheetId"]))
except Exception as e:
    check("locale sheet cycle", False, str(e)[:200])

# ---------------------------------------------------------------- 7. audit growth
audit_after = audit_lines_before()
check("audit file grew with commits", audit_before < audit_after,
      f"{audit_before} -> {audit_after}")

print()
if failures:
    print(f"RESULT: {len(failures)} FAILED -> {failures}")
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")
