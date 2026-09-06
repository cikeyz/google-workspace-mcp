# Migration guide: v1/v2.0 → v2.1

v2.0 was fully backward compatible: same 41 signatures, only additions. v2.1 changes return shapes. Read this before upgrading.

## What broke and why

List tools returned bare JSON arrays with no way to reach page 2 (the server fetched
`nextPageToken` and dropped it). v2.1 returns cursor envelopes on all 10 list tools
so agents can walk collections of any size. Token budgets also required bounded
defaults, hence per-tool `full` hatches and truncation flags.

## Breaking list (10 tools)

| Tool | v1/v2.0 shape | v2.1 shape |
|---|---|---|
| `drive_search` | `[{...}, ...]` | `{items, next_page_token, has_more, result_count}` |
| `forms_list` | `[{...}, ...]` | same envelope |
| `forms_responses` | raw API dict (`responses` key) | `{items, next_page_token, has_more, result_count}` (`items` = responses) |
| `gmail_search` | `[{...}, ...]` | same envelope + `result_size_estimate` |
| `calendar_list` | `[{...}, ...]` | same envelope |
| `people_contacts` | `[{...}, ...]` | same envelope + `total_items` |
| `tasks_lists` | `[{...}, ...]` | same envelope |
| `tasks_list` | `[{...}, ...]` | same envelope |
| `chat_spaces` | `[{...}, ...]` | same envelope |
| `chat_messages` | `[{...}, ...]` | same envelope |
| `chat_members` | `[{...}, ...]` | same envelope |

New params (all optional, backward compatible): `page_token=""`, `full=False` on most
reads, plus per-tool filters (`q`, `time_zone`, `show_completed`, `due_min/max`,
`filter_`, render options, `value_input_option`, `insert_data_option`,
`thread_key`, `config`, `locale/time_zone` grid sizes).

Behavior changes to know: `sheets_update`/`sheets_append` default to
`USER_ENTERED` (was `RAW`); `people_contacts` default fields narrowed (4 types,
`full=True` restores 10); `gmail_get` body budgeted with flags; `docs_read` walks
all tabs with a char budget; `slides_get` returns `presentation_id`/`revision_id`.

## Consumer fix recipe

```python
# before
msgs = gmail_search("is:unread", 10)
first = msgs[0]["id"]
# after
page = gmail_search("is:unread", 10)
first = page["items"][0]["id"]
while page["has_more"]:
    page = gmail_search("is:unread", 10, page_token=page["next_page_token"])
    ...process page["items"]...
```

Empty results are `{"items": [], "has_more": False, ...}`, never an error.
Same filters every iteration; only `page_token` changes.

## Restart notes

Same as any upgrade: restart the host session so the new server process loads
(opencode/Codex/ZCode/Hermes all spawn per session; Codex desktop needs a full
restart). Verify with `verify_server.py`, expect `RESULT: ALL CHECKS PASSED`.

## Rollback

Pin the `v2.0` tag (last non-breaking release), restart the host. No state
migration involved: tokens and staged ops are untouched by this upgrade.
