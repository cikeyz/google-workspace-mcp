# Google Workspace MCP — inventory & state (personal account since 2026-09-06; "ExampleCo" filename is company-era)

Snapshot date: 2026-08-11 (after the 16-tool extension session). Account switched 2026-09-06 from ExampleCo intern-hiring to you@gmail.com.

## Token / OAuth state

- Token file: `<hermes home>\google_token.json`; client secret: `google_client_secret.json`.
- OAuth client: "CK's Workspace", GCP project **example-gcp-project (000000000000)**, redirect `http://localhost:1` (was "MCP-Access" / 000000000000 under ExampleCo).
- **Scopes granted (17 — re-consent COMPLETED 2026-08-11)**: gmail.readonly, gmail.send, gmail.modify, calendar, drive, contacts.readonly, spreadsheets, documents, forms.body, forms.responses.readonly, presentations, tasks, chat.messages, chat.spaces.readonly, chat.memberships.readonly, meetings.space.created, meetings.space.readonly. All services live-verified (tasks/slides/chat/meet no longer 403).
- `setup.py --check` semantics: `AUTHENTICATED (partial): Token valid but missing N scopes` — N = scopes in SCOPES list but not granted. Exit code 0.
- Pending PKCE state: `<hermes home>\google_oauth_pending.json` (created by `--auth-url`, consumed by `--auth-code`).

## API enablement (project example-gcp-project — re-enabled 2026-09-06 on the personal client)

| API | Status 2026-08-11 | Evidence |
|---|---|---|
| Drive, Sheets, Docs, Gmail | ON (already) | live calls passed |
| Forms | ON — enabled by user; allow ~5 min propagation | SERVICE_DISABLED retried later |
| Calendar, People, Slides, Tasks, Chat, Meet | ON — user enabled all | builds resolve; calendar/people live-passed |

Console link pattern: `https://console.cloud.google.com/apis/api/<api>.googleapis.com/overview?project=example-gcp-project` (Calendar = `calendar-json.googleapis.com`).

## Return-payload policy (2026-08-11)

All read tools now return FULL API payloads (question types/required/validation/options, event attendees, message headers+labels+attachments, raw person resources, etc.) instead of hand-picked subsets. Exceptions: `google_docs_read` (structured text extraction + revisionId), `google_slides_get` (per-slide text/notes/element inventory), `google_gmail_get` body (plain-text extraction, html truncated at 2000 chars) — raw payloads there are too large to be useful. `google_drive_download` is inherently a local-file tool. Write tools return the full created/updated resource.

## Full tool inventory (41)

- `google_auth_status`
- Staged-write controls: `google_write_commit`, `google_write_cancel`, `google_write_list_staged`
- Sheets: `google_sheets_metadata`, `google_sheets_read`, `google_sheets_update`, `google_sheets_append`, `google_sheets_create`
- Drive: `google_drive_search` (full query syntax!), `google_drive_get`, `google_drive_download`, `google_drive_upload`, `google_drive_create_folder`, `google_drive_share`, `google_drive_trash`
- Docs: `google_docs_read`, `google_docs_create`, `google_docs_append`
- Forms: `google_forms_list`, `google_forms_get`, `google_forms_responses`
- Gmail: `google_gmail_search`, `google_gmail_get`
- Calendar: `google_calendar_list` (defaults now→+7d), `google_calendar_get`, `google_calendar_create` (ISO 8601 WITH tz), `google_calendar_delete`
- People: `google_people_contacts`
- Slides: `google_slides_get` (per-slide text), `google_slides_create`
- Tasks: `google_tasks_lists`, `google_tasks_list` (`@default` = default list), `google_tasks_create`, `google_tasks_update` (completed/needsAction), `google_tasks_delete`
- Chat: `google_chat_spaces`, `google_chat_messages`, `google_chat_send`
- Meet: `google_meet_create_space`, `google_meet_get_space`

## Field notes (errors seen & their real causes)

- `google_drive_search("EXAMPLECO BATCH 6 INTERNS")` → 400 Invalid Value. Correct: `"name contains 'EXAMPLECO'"`.
- `google_forms_get` → 403 SERVICE_DISABLED even after console enablement; resolved by waiting minutes. **RESOLVED 2026-08-11 (same day, minutes later):** user re-enabled Forms API in console → `google_forms_get` PASSED live on form `YOUR_FORM_ID`. Lesson: enablement propagation is NOT guaranteed — always prove Forms works with a live call after enabling.
- `google_tasks_lists()` pre-consent → 403 "Request had insufficient authentication scopes" — EXPECTED; proves code path works.
- `build(name, ver)` without `credentials=` → DefaultCredentialsError (ADC fallback); always pass `credentials=_get_creds()` like `_svc()` does.
- FastMCP introspection: `mcp._tool_manager.list_tools()` → list of tool objects; `.name` attribute (NOT a dict of name→tool).

## Useful Drive artifacts found in the account (2026-08-11)

- "EXAMPLECO BATCH 6 FORM" — id `YOUR_FORM_ID` (public title: "EXAMPLECO BATCH 6 INTERNS"); responses land in "Form Certicode Batch 6 Intern_Master_List" (tabs `Batch 6`, `Dropdown Lists`).
- "ExampleCo Hiring Guide" doc (id `YOUR_DOC_ID`) — hiring SOP + templates; acceptance email links `forms.gle/xSG6zxZZtTbCYJbNA` onboarding form.
- NDA PDFs and DTR templates for interns live in Drive (folder parents visible via `google_drive_search`).
