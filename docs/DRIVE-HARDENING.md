# Drive v3 hardening and timestamp uploads

Reviewed against the official Google Drive API v3 and Google Auth Platform
documentation on **2026-11-22**. This guide applies to the Python MCP server
in \`server.py\`, not to ChatGPT's separate Google Drive connector.

## Google Cloud Console checklist

1. In [Google Cloud Console](https://console.cloud.google.com/apis/dashboard),
   select the project containing the OAuth client used by this MCP.
2. Under **APIs & Services > Enabled APIs & services**, confirm the **Google Drive
   API** is enabled. The MCP uses Drive **v3**.
3. Open **Google Auth Platform > Branding / Audience / Data Access**. Verify the
   app's audience, consent screen, and requested scope. For a personal local
   app, use the narrowest scopes that meet your requirements.
4. The existing full-Workspace toolset requests
   \`https://www.googleapis.com/auth/drive\` in \`setup/setup.py\`.
   No additional OAuth scope is required for timestamp metadata or for the
   changed Drive v3 operations. **This is a restricted scope**. A focused
   uploader may instead use \`drive.file\`, which grants access to files
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

\`google_drive_upload\` now accepts optional \`created_time\` and
\`modified_time\` parameters, for example:

\`\`\`text
local_path: C:\Users\USER\Documents\report.pdf
name: report.pdf
created_time: 2026-09-15T10:30:00+08:00
modified_time: 2026-09-15T10:30:00+08:00
\`\`\`

Both values **must** be RFC 3339 timestamps with explicit time zone offsets.
The tool only **stages** the upload; review the preview, then call
\`google_write_commit(operation_id)\`. It checks the local file hash again
using streaming reads (bounded memory), rechecks the destination folder, and
performs a resumable upload.

The commit returns \`createdTime\` and \`modifiedTime\` from the API along
with \`timestamp_verification\`, comparing instants even if Google renders
them in UTC. Always inspect the **actual** returned timestamps rather than
assuming Google accepted a requested value.

\`google_drive_create_file\` and \`google_drive_copy\` also accept creation
and modification timestamps. \`google_drive_update\` supports updating
\`modified_time\`, **not** an existing file's \`createdTime\`.

**Important:** Drive metadata is not the same as the actual date an upload
occurred. The Drive Activity API, event logs, and audit records may expose
the real operation date. This feature is not an audit-history editor.

## Other Drive changes

- **Safer uploads:** streaming SHA-256, regular-file checks, destination
  validation, resumable transfer with bounded retries.
- **Safer downloads:** sanitized, ID-prefixed names; unique temporary files;
  failed downloads do not leave completed-looking partial files.
- **Explicit public sharing:** passing no email now refuses to stage unless
  \`anyone_with_link_acknowledged=True\` is provided. This is an intentional
  breaking safety change; audit existing agent prompts and E2E test scripts.
- **Stronger staged writes:** copies, renames, moves, trash, and shares
  revalidate their target state before commit. A conflict requires re-staging.
- **Shared drives:** Drive requests use \`supportsAllDrives=True\`.
  Searches and recent-file reads include shared-drive items; pass \`drive_id\`
  to search a particular shared drive.
- **Pagination:** permissions list pages until complete, bounded by a safety
  limit. \`google_drive_search\` and \`google_drive_recent\` return
  \`incomplete_search\` to expose incomplete Drive result sets.
- **Metadata scope:** ordinary operations request selected fields; full
  read tools still support \`full=True\` for debugging.

## Limits and deliberate exclusions

- The main MCP runs on the machine where its \`local_path\` exists. A remote
  MCP server cannot read an arbitrary path on a user's laptop.
- Google-native exports have format/size restrictions; binary downloads and
  exports are separate operations.
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

## Testing

Offline regression tests:

\`\`\`sh
python -m unittest discover -s setup/tests -p test_drive_unit.py -v
\`\`\`

CI installs the pinned Python dependencies and runs these without
connecting to Google. The separate \`setup/tests/test_server.py\` exercises
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
