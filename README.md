# Google Workspace MCP

A Model Context Protocol server that gives coding agents full, honest access to Google Workspace: Gmail, Drive, Docs, Sheets, Slides, Forms, Calendar, People, Tasks, Chat, and Meet. 41 tools. Every read returns the whole API resource, every write is staged, reviewed, then committed.

## Why this one

- **Whole context**: read tools return full API payloads, not trimmed summaries. An agent deciding on incomplete data makes bad calls.
- **Staged writes**: no write touches Google directly. Each write stages a preview with checks and returns an `operation_id`. You review, then `google_write_commit` applies (single-use, revalidates first) or `google_write_cancel` discards.
- **Harness-agnostic**: runs anywhere Python runs. State home is one env var (`GOOGLE_WORKSPACE_HOME`). Tested with OpenCode, Codex, Zed-era ZCode configs, and Hermes-style stdio hosts.

## Quickstart

1. Create an OAuth client: any GCP project, `APIs & Services` → `OAuth consent screen` (External), then `Credentials` → `OAuth client ID` → `Desktop app`. Download the JSON.
2. Enable the 11 APIs on that project: Drive, Gmail, Calendar, Sheets, Docs, Slides, Forms, Tasks, People, Chat, Meet. (`setup/setup.py` tells you which call failed if you miss one.)
3. Install deps into a venv: `pip install -r requirements.txt` (Python 3.11).
4. Store the client: `python setup/setup.py --client-secret /path/to/client_secret.json`.
5. Log in: `python setup/setup.py --auth-url`, open the URL, approve all scopes, paste back the redirect URL via `python setup/setup.py --auth-code '<url>'`.
6. Verify: `python setup/tests/verify_server.py`. Expect `RESULT: ALL CHECKS PASSED`.
7. Point your host at it (stdio): command = venv python, args = `server.py`, env = `GOOGLE_WORKSPACE_HOME` → your state dir.

Set `GW_FIXTURE_DOC_ID` / `GW_FIXTURE_FORM_ID` to your own readable Doc/Form to unlock the full `setup/tests/test_server.py` battery; fixture checks SKIP when unset.

## Staged-write protocol

`stage → preview + checks → commit (revalidates) or cancel`. Commits are single-use, expire after 24h, cap at 20 concurrent, fail closed in cron sessions, and append to `logs/google-write-audit.jsonl`. Destructive targets always show full identity in previews.

## Layout

- `server.py` — the MCP server (FastMCP, stdio)
- `setup/setup.py` — OAuth setup (`--check`, `--auth-url`, `--auth-code`, `--revoke`)
- `setup/tests/` — `verify_server.py` (quick battery), `test_server.py` (full E2E with self-cleanup), `check_conditional_formats.py` (Sheets probe)
- `docs/` — maintainer skill notes plus server inventory

## Security notes

- `state/` holds a Gmail-capable OAuth grant. Keep it user-private (`icacls` / `chmod 700`), never commit it (see `.gitignore`).
- Testing-mode OAuth clients need weekly re-consent unless the app is verified.
- The audit log records write metadata. Treat it as sensitive.

## License

MIT. See `LICENSE`.
