# Drive v3 hardening and timestamp uploads

Reviewed against the official Google Drive API v3 and Google Auth Platform
documentation on **2026-11-22**. This guide applies to the Python MCP server
in `server.py`, not to ChatGPT's separate Google Drive connector.

## Google Cloud Console checklist

1. In [Google Cloud Console](https://console.cloud.google.com/apis/dashboard),
   select the project containing the OAuth client used by this MCP.
2. Under **APIs & Services > Enabled APIs & services**, confirm the **Google Drive
   API** is enabled. The MCP uses Drive **v3**.
3. Open **Google Auth Platform > Branding / Audience / Data Access**. Verify the
   app's audience, consent screen, and requested scope. For a personal local
   app, use the narrowest scopes that meet your requirements.
4. The existing full-Workspace toolset requests
   `https://www.googleapis.com/auth/drive` in `setup/setup.py`.
   No additional OAuth scope is required for timestamp metadata or for the
   changed Drive v3 operations. **This is a restricted scope**. A focused
   uploader may instead use `drive.file`, which grants access to files
   created/opened by the app. That cannot transparently replace broad
   Drive access required by this MCP's search/administrative features.
5. Review the OAuth client's application type and credentials. Never check in
   client secrets, tokens, or refresh tokens. For external OAuth clients in
   **Testing**, Google documents seven-day refresh token expiry for broad
   Workspace scopes. Published and eligible personal-mode apps differ; check
   the [current Google Auth Platform guidance](https://support.google.com/cloud/answer/15544987).
6. Under **Google Drive API > Quotas & System Limits**, inspect request quotas.
   The current Drive guide describes per-minute/per-user/per-project quotas
   and quota-unit billing policies. These may change: verify in your project.
7. Restart your MCP client after upgrading the server so new tools/arguments
   are discovered. The staged-write gate remains mandatory.

This repository review **cannot prove the live configuration of your private
Cloud project**. Verify the checklist in the actual console; do not assume its
settings match the documentation.

## Upload preserving creation/modification dates

`google_drive_upload` now accepts optional `created_time` and
`modified_time` parameters, for example:

```text
local_path: C:\Users\USER\Documents\report.pdf
name: report.pdf
created_time: 2026-09-15T10:30:00+08:00
modified_time: 2026-09-15T10:30:00+08:00
```

Both values **must** be RFC 3339 timestamps with explicit time zone offsets.
The tool only **stages** the upload; review the preview, then call
`google_write_commit(operation_id)`. It checks the local file hash again
using streaming reads (bounded memory), rechecks the destination folder, and
performs a resumable upload. A Drive-generated file ID is allocated while
staging (without creating a file), included in the preview, and supplied at
commit. On an ambiguous commit failure, query that ID before any retry.
Drive's 409 conflict behavior prevents duplicating the same pre-generated ID.

The commit returns `createdTime` and `modifiedTime` from the API along
with `timestamp_verification`, comparing instants even if Google renders
them in UTC. Always inspect the **actual** returned timestamps rather than
assuming Google accepted a requested value.

`google_drive_create_file` and `google_drive_copy` also accept creation
and modification timestamps. `google_drive_update` supports updating
`modified_time`, **not** an existing file's `createdTime`.

**Important:** Drive metadata is not the same as the actual date an upload
occurred. The Drive Activity API, event logs, and audit records may expose
the real operation date. This feature is not an audit-history editor.

## v2.6 extended Drive coverage (17 tools)

The release expands Drive from 13 to **30 Drive tools**, and the complete MCP
from 73 to **90 tools** (4 prompts unchanged), without adding OAuth scopes.

- `google_drive_about`: user/storage capabilities and import/export formats.
- `google_drive_shared_drives`: paginate shared drives, not just their files.
- `google_drive_changes_start` / `google_drive_changes`: cursor-driven
  change polling. Persist `new_start_page_token` when paging completes;
  user and shared-drive streams are distinct and must not be mixed.
- `google_drive_revisions`: list available revisions; editor UI revision
  history may be more extensive than Drive's revision API.
- `google_drive_comments`: list Drive comments with mandatory field masks.
- `google_drive_file_labels`: read file label assignments through Drive v3,
  without requiring the separate Drive Labels API scope for label definitions.
- `google_drive_restore`: staged untrash, guarded by `canUntrash`.
- `google_drive_shortcut`: staged shortcut creation; this does not copy content.
- `google_drive_permission_remove`: explicit acknowledgment, staged
  permission revocation; privileged owner/organizer removal deliberately denied.
- `google_drive_metadata_update`: staged description/starred and JSON
  property-map changes, with a narrow allowlist and bounded input.
- `google_drive_comment_create` / `google_drive_comment_reply`: staged,
  unanchored comments and replies with pre-commit state checks.

### Binary replacement, history and typed labels

- `google_drive_replace_content(file_id, local_path, replace_acknowledged=True,
  keep_revision_forever=False)`: stages replacing an **existing binary**
  Drive file's content with resumable media. This creates a new revision while
  retaining the same Drive file ID, name, location, sharing and comments.
  Both the local SHA-256/size/mtime and remote version/checksum/MIME are
  revalidated at commit. Not supported on native Docs, Sheets, Slides, folders
  or shortcuts. A failed or timed-out commit may have changed Drive already:
  inspect the file/revisions before re-staging.
- `google_drive_revision_pin(file_id, revision_id,
  keep_forever_acknowledged=True)`: stages adding permanent Keep Forever
  retention to an existing binary revision, enabling historical downloads.
  Pinned revisions count toward Drive storage and the 200-revision ceiling.
  Retention is not simply reversible: removing a pinned revision requires
  deleting that revision. Pinning must be deliberately approved.
- `google_drive_revision_download(file_id, revision_id)`: downloads a
  **Keep Forever** binary revision to an ID- and revision-prefixed unique
  local file. It does not overwrite earlier downloads and checks the
  revision MD5 when Drive provides one. Google only supports downloading
  pinned binary revisions through `revisions.get` with `alt=media`.
  Google-native editor revision exports require a different flow and are
  outside this tool's scope.
- `google_drive_label_modify(file_id, label_id, remove_label=False,
  field_modifications_json="")`: stages applying an empty/fieldless label,
  removing a label, setting typed field values or unsetting specific fields
  through `files.modifyLabels`. The JSON array accepts
  `fieldId` plus exactly one of `setTextValues`,
  `setSelectionValues`, `setDateValues`, `setIntegerValues`,
  `setUserValues`, or `unsetValues:true`.
  Selection field IDs and choice IDs come from the Drive Labels schema,
  a separate API. Drive enforces the field type, multiplicity and user
  permissions. Label edits revalidate the current selected label assignment
  and the file version before commit.

Official references:
[files.update](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/update),
[revisions.get](https://developers.google.com/workspace/drive/api/reference/rest/v3/revisions/get),
[revision download restrictions](https://developers.google.com/workspace/drive/api/guides/manage-downloads),
[files.modifyLabels](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/modifyLabels).

All new mutation tools use the existing `google_write_commit` protocol.
Auth remains the existing `drive` scope; a new token or API enablement is
not necessary for these Drive v3 calls if the current token is valid.

**Still deliberately outside this release:** permanent file deletion,
transfer of ownership, shared-drive administration, Drive Labels schema
creation/editing, Enterprise approval flows, long-running Google Vids download,
Workspace Events subscriptions/webhook delivery, and Drive Activity API
history. These involve destructive operations, separate API surfaces,
additional scopes, asynchronous delivery, or deployment infrastructure.
They should not be treated as implemented or production-verified.

## Other Drive changes

- **Upload integrity:** binary uploads reserve Drive IDs before commit; the
  returned SHA-256 digest is compared against the staged bytes where available.
- **Safer uploads:** streaming SHA-256, regular-file checks, destination
  validation, resumable transfer with bounded retries.
- **Safer downloads:** sanitized, length-bounded, ID-prefixed names; unique temporary files;
  failed downloads do not leave completed-looking partial files; where Drive
  supplies a SHA-256 checksum, it must match before publication. Repeat
  downloads use collision-resistant names instead of overwriting local files.
- **Fail-closed capabilities:** missing/false capabilities do not authorize
  sharing, copying, trashing, editing, or adding folder children.
- **Explicit public sharing:** passing no email now refuses to stage unless
  `anyone_with_link_acknowledged=True` is provided. This is an intentional
  breaking safety change; audit existing agent prompts and E2E test scripts.
- **Stronger staged writes:** copies, renames, moves, trash, and shares
  revalidate their target state before commit. Single-parent moves automatically
  remove the prior parent when adding a destination; copying folders is refused.
  A conflict requires re-staging.
- **Shared drives:** Drive requests use `supportsAllDrives=True`.
  Searches and recent-file reads include shared-drive items; pass `drive_id`
  to search a particular shared drive.
- **Pagination:** permissions list pages until complete, bounded by a safety
  limit. `google_drive_search` and `google_drive_recent` return
  `incomplete_search` to expose incomplete Drive result sets.
- **Metadata scope:** ordinary operations request selected fields; full
  read tools still support `full=True` for debugging.

## Limits and deliberate exclusions

- The main MCP runs on the machine where its `local_path` exists. A remote
  MCP server cannot read an arbitrary path on a user's laptop.
- Google Workspace exports through `files.export` have a documented **10 MB
  output limit**, independent of the larger binary upload/download limits.
- Inline text reads decode UTF-8 incrementally, paginate by Unicode
  characters, and report `text_chars_exact` to distinguish complete lengths
  from lower bounds when a download stops early.
- Binary downloads and Google-native exports are distinct operations.
- Remote commit failures can be ambiguous. A failed upload should be
  followed by a Drive search/metadata check before re-staging, to avoid
  accidental duplicates. Pre-generated IDs can support stronger
  idempotency in future for compatible binary uploads.
- Shared-drive permissions and capabilities depend on the account's role and
  organization policy; API parameters alone do not confer permissions.
- The MCP is not a transactional database. Revalidation reduces stale
  writes but cannot eliminate concurrent changes between checks and writes.
- Public-sharing and timestamp changes can be restricted by organization
  policy even when the API accepts the request structure.

## 2026 Drive quota and egress model

Google's 2026 Drive API quota framework counts **quota units**, not only raw
request counts. Google's published limits describe 1,000,000 units per minute
per project, 325,000 per minute per user per project, 400 million daily units
before usage-based charges, and 1 TB daily project egress. A binary upload
also faces per-user 750 GB daily upload/copy limits and a 5 TB maximum file.
These figures may change; verify your project's actual quota and billing state
in Cloud Console rather than assuming API use remains unconditionally free.

The separate Google-hosted Workspace **MCP developer preview** has its own
OAuth-agent configuration requirements. This repository uses the Drive v3
REST API directly through a self-hosted stdio MCP and should not be assumed
to be the Google-hosted preview endpoint.

## Testing

Offline regression tests:

```sh
python -m unittest discover -s setup/tests -p test_drive_unit.py -v
```

CI installs the pinned Python dependencies and runs these without
connecting to Google. The separate `setup/tests/test_server.py` exercises
real Workspace APIs and must only be run deliberately with disposable
fixtures and explicit credentials; it is not executed in public CI.

## Sources

- [Drive file metadata and timestamp fields](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)
- [Drive files.create](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/create)
- [Drive files.update](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/update)
- [Resumable uploads and pre-generated IDs](https://developers.google.com/workspace/drive/api/guides/manage-uploads)
- [Shared-drive support](https://developers.google.com/workspace/drive/api/guides/enable-shareddrives)
- [Drive API scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [Drive quota, rate limiting, and billing](https://developers.google.com/workspace/drive/api/guides/limits)
- [Google Auth Platform configuration](https://developers.google.com/workspace/guides/configure-oauth-consent)
- [OAuth refresh-token expiration rules](https://developers.google.com/identity/protocols/oauth2)
