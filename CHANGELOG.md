# Changelog

## v2.0.0 (2026-09-06) — non-breaking

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
