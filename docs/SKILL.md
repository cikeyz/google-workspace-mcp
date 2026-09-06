---
name: google-workspace-mcp
description: "Maintain the personal Google Workspace MCP server."
version: 1.0.0
author: Hermes curator
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [Google, MCP, OAuth, Personal, GCP, Scopes]
    related_skills: [google-workspace, hermes-mcp-servers]
---

# Google Workspace MCP (Personal)

The Hermes profile runs a Google Workspace MCP server (`google-workspace`) authenticated as you's personal Google account (`you@gmail.com`). Switched from the ExampleCo intern-hiring account on 2026-09-06; company token and client secret kept as `.exampleco-backup` files in hermes home, nothing revoked. This skill covers maintaining that integration: tool inventory, OAuth scopes, GCP API enablement, re-consent flow, and verification. Use it whenever the google_workspace MCP tools are extended, misbehave, or return scope/API errors.

## When to Use

- Extending the google-workspace MCP server with new tools/services (add to server.py + setup.py SCOPES + verify script).
- Re-consent / scope upgrade flows, or debugging `403 insufficient authentication scopes` / `SERVICE_DISABLED` on google_workspace tools.
- Enabling Google APIs in the GCP console for this project (example-gcp-project).
- Any session touching `mcp_servers\google_workspace\` or the personal Google account token.

## Key paths (canonical home — reference copy; live skill stays in the Hermes skills tree until Phase 2)

- Server: `C:\Users\YOU\.agents\mcps\google-workspace\server.py` (FastMCP, `mcp==1.26.0` pinned)
- Interpreter: `C:\Users\YOU\.agents\mcps\google-workspace\.venv\Scripts\python.exe` — MUST stay self-sufficient (the MCP client spawns a filtered env, no PYTHONPATH)
- Config per harness (all launch the canonical server; Hermes still points at its old copy until Phase 2): opencode `opencode.json`, Codex `config.toml`, ZCode `config.json`, Hermes `config.yaml` → `mcp_servers.google-workspace`
- Auth: `GOOGLE_WORKSPACE_HOME` → state dir holding `google_token.json` + `google_client_secret.json` (legacy `HERMES_HOME` still honored as fallback); OAuth client "CK's Workspace" (GCP project example-gcp-project / 000000000000), redirect `http://localhost:1`
- Scope source of truth: canonical `setup/setup.py` SCOPES list (also the auth tool: `--auth-url` / `--auth-code` / `--check`)
- Tests: canonical `setup/tests/verify_server.py` (quick) + `test_server.py` (full E2E) — run with `HERMES_HOME` set to the state home

## Service matrix (11 services, 54 tools — v2.0 2026-09-06)

| Service | API to enable (console) | Scope | Tools |
|---|---|---|---|
| sheets v4 | Sheets API | spreadsheets | metadata, read, update, append, create, conditional_formats |
| drive v3 | Drive API | drive | search, get, download, upload, create_folder, share, trash, permissions, copy, update |
| docs v1 | Docs API | documents | read, create, append |
| forms v1 | Forms API | forms.body, forms.responses.readonly | list, get, responses |
| gmail v1 | Gmail API | gmail.readonly, gmail.send, gmail.modify | search, get, thread_get, attachment_download, send |
| calendar v3 | Calendar API | calendar | list, get, create, delete, patch, freebusy |
| people v1 | People API | contacts.readonly | contacts, search, get |
| slides v1 | Slides API | presentations | get, create |
| tasks v1 | Tasks API | tasks | lists, list, get, create, update, delete |
| chat v1 | Chat API | chat.messages, chat.spaces.readonly, chat.memberships.readonly | spaces, messages, send, members |
| meet v2 | Meet API | meetings.space.created, meetings.space.readonly | create_space, get_space |

Console enable link pattern: `https://console.cloud.google.com/apis/api/<api>.googleapis.com/overview?project=example-gcp-project` — Calendar uses `calendar-json.googleapis.com`. Full inventory + enablement state: `references/server-inventory.md` (company-era filename, contents still valid).

## Scope upgrade / re-consent flow (verified 2026-08-11)

1. Add new scopes to the bundled skill's `setup/setup.py` SCOPES list — that file is the scope source of truth for both setup and the MCP server's token.
2. Generate the URL **with GOOGLE_WORKSPACE_HOME set** (otherwise setup.py resolves a default home and the pending PKCE state lands in the wrong place):
   `env -u PYTHONPATH .venv/Scripts/python.exe <skill>/setup/setup.py --auth-url`
3. User opens URL (uses `prompt=consent`, forcing a full re-consent screen with all scopes), approves, pastes back the `http://localhost:1/?code=...` redirect URL.
4. Exchange: `--auth-code '<url>'` — overwrites the token file and stores only the scopes actually granted.
5. Verify with `--check`: `AUTHENTICATED (partial)` listing missing scopes is EXPECTED until consent completes; the missing list must match exactly the newly added scopes.
6. Do NOT `--revoke` first — the exchange overwrites the token, so a failed consent leaves the old token working.
7. New tools only appear in a NEW Hermes session (one server process per session).

## Write safety: staged-write protocol (2026-08-11)

No google_* write tool touches Google directly. Each one STAGES: it snapshots the current state, runs deterministic checks, and returns `operation_id` + `preview` + `checks` WITHOUT applying. The agent reviews the preview, then applies via `google_write_commit(operation_id)` (single-use; revalidates live state first — e.g. refuses if the range/event/task changed since staging) or discards via `google_write_cancel(operation_id)`. `google_write_list_staged()` shows pending ops. Read tools are never gated.

Hardening (2026-08-11): `google_write_commit` **fails closed in cron** (`HERMES_CRON_SESSION` set → blocked unless `HERMES_ALLOW_CRON_WRITES=1`); staged ops **expire after 24h** and cap at **20 concurrent**; commits are logged to `<HERMES_HOME>/logs/google-write-audit.jsonl` by both the server and the `google-write-guard` plugin (audit-only, no prompts).

**Testing:** `setup/tests/test_server.py` is the full battery (54 tools, live reads across services, E2E stage→commit→verify→cleanup per write tool with `GW-TEST-` artifacts, double-commit/cron/TTL/cap semantics, audit growth). `setup/tests/verify_server.py` is the quick 5-check battery. Run both after every server change.

**MAINTENANCE RULE: when adding a write tool to server.py, (1) implement it with the `_stage(...)` pattern (apply_fn + checks + preview + optional revalidate), (2) add its name to `AUDITED_TOOLS` in the plugin, (3) add it to both test scripts' expected tool sets, (4) run `test_server.py`.**

## Pitfalls

- **Emoji/special-char tab names (ExampleCo sheets)**: team tabs are emoji-prefixed, e.g. `🔄 ALL_COMBINED_MASTER`, `⛏️ Anne (US, UK, ES, AU)`. Safest: single-quote the tab name in A1 notation: `'🔄 ALL_COMBINED_MASTER'!A1:N10`. Verified 2026-08-13: an emoji-only name with no spaces (`🔄 ALL_COMBINED_MASTER!A1:L2`) ALSO parsed unquoted, so emoji alone is not the breaker — spaces/parentheses are (the miner tabs like `⛏️ Anne (US, UK, ES, AU)` need quotes). When any range parse fails, call `google_sheets_metadata` first — `sheets[].properties.title` is the authoritative tab-name list (also gives sheetIds, index, frozen rows, grid size) and reveals emoji/whitespace not visible in task docs.
- **Conditional formats are NOT exposed by the MCP tools**: to verify sheet-side rules (e.g. playbook-promised duplicate highlighting) use the read-only probe `setup/tests/check_conditional_formats.py <SPREADSHEET_ID>` (spreadsheets.get fields `sheets(properties(sheetId,title),conditionalFormats)`; prints per-tab rules, "no conditional formats" = gate absent). Run with `env -u PYTHONPATH` + `C:/...` paths.
- **PYTHONPATH masking**: git-bash exports PYTHONPATH → hermes-agent venv, which shadows the .venv. Always `env -u PYTHONPATH` when running the .venv python for tests; a "wrong" package path in a traceback usually means this, not a missing install.
- **google-auth >= 2.55 client_id read-only**: `creds.client_id = ...` raises `AttributeError: property 'client_id' ... has no setter`. The refresh branch of `_get_creds()` must REBUILD the Credentials from merged token+client info (`Credentials.from_authorized_user_info({**info, "client_id": ...})`), never mutate attributes. Token-expiry is the only trigger — a valid token hides this bug until the first refresh.
- **MSYS `~` mangling**: `~/AppData/...` passed as a python arg becomes `C:\c\Users\...`. Use `C:/Users/...` Windows-style paths.
- **API enablement propagation**: `403 SERVICE_DISABLED` right after enabling an API in the console is normal — retry after ~5 minutes before touching the console again.
- **drive search query**: `query` is a full Drive API query string (`name contains 'X'`); bare words → `400 Invalid Value`.
- **Pre-consent 403s**: tasks/slides/chat/meet return `403 insufficient authentication scopes` until the new scopes are consented — that is the designed failure mode, not a code bug.
- **FastMCP introspection**: `mcp._tool_manager.list_tools()` returns a LIST of tool objects (use `.name`), not a dict.
- **Invocation pattern**: `env -u PYTHONPATH` + explicit `C:/...` paths are the reliable way to run this venv's python from git-bash.

## Verification

Run `setup/tests/verify_server.py` with the .venv python (`env -u PYTHONPATH`). Expected: 54 tools registered, calendar list + people contacts + tasks lists PASS live (all 17 scopes granted 2026-08-11). Extend the script when adding tools.

## User preference

For Google Cloud console work, hand over clickable console enable links by default. CLI tools (e.g. gcloud) are allowed when the user asks — gcloud 583.0.0 was installed machine-wide on 2026-09-06 at you's request (supersedes the 2026-08-11 console-only note).

## References

- `references/server-inventory.md` — full tool inventory, API enablement status, token scope state
- `setup/tests/verify_server.py` — re-runnable verification battery for the server
- `setup/tests/check_conditional_formats.py` — read-only per-tab conditional-format probe (see Pitfalls)
