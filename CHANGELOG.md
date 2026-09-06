# Changelog

## v2.3.0 (2026-09-06) — First-party parity + MCP SDK 2.x

Non-breaking: 17 new tools (54 to 71) + 4 prompts, no signature removals.
Existing callers keep working; `google_chat_send` gains an optional
`thread_name`, calendar reads/writes gain an optional `calendar_id`,
`google_calendar_patch` gains optional `attendees`/`visibility`,
`google_chat_messages` gains optional `filter_`/`order_by`.

- SDK: `mcp` 1.29.1 to 2.1.1 (`FastMCP` to `MCPServer`; speaks both protocol
  eras, 2025-06-18 and 2026-07-28, via `server/discover`). Sync tools now run
  on worker threads; stdio serves one request at a time so the in-memory
  staged-op store is unaffected. `RuntimeError` still surfaces as
  model-visible tool errors (verified).
- Gmail: labels list, staged label add/remove, thread-level search.
- Drive: recency listing, inline text reads (Docs/Slides text, Sheets CSV),
  staged inline file creation.
- Sheets: staged row/column insertion. Formulas stay on `google_sheets_update`
  with USER_ENTERED + allow_formulas (Google does not guard injection; we do).
- Calendar: calendar discovery, free-text event search, staged RSVP, free-slot
  suggestions over freebusy.
- Chat: conversation search (list+filter; `spaces.search` 400s for consumer
  accounts), staged read/unread, thread-reply fix (`messageReplyOption` plus
  `thread_name` for human-created threads; old `thread_key` silently started
  new threads).
- People: own profile (new `userinfo.profile` scope), server-side contact
  search. Directory search omitted (Workspace-only, 403s on consumer accounts).
- Universal: `google_universal_search` fans one query across Drive/Gmail/
  Calendar/Contacts with per-source isolation (Chat opt-in, capped).
- Prompts: triage_inbox, prep_meeting_brief, summarize_thread, find_anything
  (instruction templates only; they call the tools above, fetch nothing).
- Deferred: generic Docs/Slides batchUpdate writers, MCP Tasks extension
  (Python SDK has no runtime yet; staged writes already work on every client),
  Resources primitive (tools cover all reads).
- New scopes (re-auth via setup.py --auth-url): `userinfo.profile`,
  `chat.users.readstate`. No scopes narrowed: full Drive/Calendar stay because
  trash/share/permissions/patch are the product.
- Rollback: pin the v2.2 tag and restart the host.

## v2.2.0 (2026-09-06) — Python 3.14 baseline

Non-breaking. Documented and CI-tested baseline moves from 3.11 to 3.14
(floor stays 3.11+, no ceiling). Zero runtime code changes: the codebase
already used `X | Y` syntax, `datetime.now(timezone.utc)`, and no asyncio
or removed stdlib. Verified with stdio `initialize` + `tools/list` smoke
on 3.14.7 and `py_compile` on 3.11.15.

- Deps re-pinned and tested on 3.14: `mcp` 1.26.0 to 1.29.1 (first v1 line
  with declared 3.14 support), `google-api-python-client` to 2.200.0,
  `google-auth` to 2.57.1, `google-auth-oauthlib` to 1.4.1,
  `google-auth-httplib2` to 0.4.2, `filelock` 3.16.1 to 3.32.5.
  `httplib2`/`pyasn1` unchanged (current). All pure-Python, no 3.14 blocks.
- CI now runs the hygiene battery on a `[3.11, 3.14]` matrix (floor + head).
- New `.python-version` pins 3.14 for contributors and `uv`/`pyenv`.
- Rollback: re-pin to the v2.1 requirements and delete `.python-version`.

## v2.1.0 (2026-09-06) — BREAKING list envelopes

All 10 list tools return `{items, next_page_token, has_more, result_count}` instead of
bare arrays (plus `result_size_estimate` on Gmail, `total_items` on People). New
`page_token`/`full` params, raised caps (Drive 1000, Gmail 500, Calendar 2500),
per-tool fields masks, truncation flags, Sheets render/input options (USER_ENTERED
default), chat thread replies, Meet config, Drive copy/update, Gmail send, calendar
patch, and 9 more read tools. See MIGRATION-v2.md. Non-breaking consumers: none,
every list caller must switch to `["items"]`.

Added (13 tools, 41 to 54, no signature changes to existing tools):
- Reads: gmail thread get, gmail attachment download, people search/get, tasks get, drive permissions audit, chat members, calendar freebusy, sheets conditional formats.
- Staged writes: gmail send, calendar patch, drive copy, drive file update (rename/move).

Fixed:
- `tasks_update` uses PATCH with title/notes/due instead of destructive full-replace PUT.
- `docs_append` recomputes insert offset at apply time with `writeControl` revision lock.
- `drive_upload` pins file hash at stage, revalidates before upload; resumable chunked uploads; parent folder verified.
- `chat_send` uses server-generated message IDs and revalidates space membership; `calendar_create` validates ISO8601 timezones and stamps client iCalUIDs.

Performance: process-level credential plus discovery-doc caches, Gmail batch fan-out, per-call timeouts with bounded retries, streaming downloads.
Safety surface: `title` plus readOnly/destructive/idempotent/openWorld annotations on all tools, server instructions.
Security: atomic owner-only credential writes, locked refresh, `revoke()` covers refresh tokens, PKCE pending TTL plus cleanup, auth-code via stdin/file, redacted audit events (stage/cancel/failures now logged), state dir locked to the user.

## v1 (2026-09-06)

Initial public release. 41 tools across Gmail, Drive, Docs, Sheets, Slides, Forms, Calendar, People, Tasks, Chat, Meet, plus auth status and staged-write controls. Full-payload reads, staged-write protocol with revalidation, per-process state home via `GOOGLE_WORKSPACE_HOME`.
