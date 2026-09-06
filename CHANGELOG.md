# Changelog

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
