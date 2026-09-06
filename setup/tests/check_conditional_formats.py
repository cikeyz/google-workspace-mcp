"""READ-ONLY: list conditional-format rules per tab of a spreadsheet.

The google_workspace MCP tools do not expose conditional formats; this probe
verifies whether sheet-side gates (e.g. playbook-promised duplicate
highlighting) actually exist.

Usage (git-bash, from the mcp_servers/google_workspace dir):
    env -u PYTHONPATH .venv/Scripts/python.exe scripts/check_conditional_formats.py <SPREADSHEET_ID>

Use C:/Users/... Windows-style paths (MSYS mangles /c/Users/... into C:\\c\\Users\\...).
Prints per-tab rules; "no conditional formats" = the gate does not exist.
Verified 2026-08-13 against the ExampleCo master hub (all 8 tabs: no rules).
"""
import json
import os
import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

HOME = Path(os.environ.get("HERMES_HOME", Path.home() / "AppData/Local/hermes"))
TOKEN = str(HOME / "google_token.json")


def main(spreadsheet_id: str) -> None:
    with open(TOKEN, encoding="utf-8") as fh:
        info = json.load(fh)
    creds = Credentials.from_authorized_user_info(info)
    svc = build("sheets", "v4", credentials=creds)
    res = (
        svc.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title),conditionalFormats)",
        )
        .execute()
    )
    for sheet in res.get("sheets", []):
        title = sheet["properties"]["title"]
        gid = sheet["properties"]["sheetId"]
        cfs = sheet.get("conditionalFormats", [])
        if cfs:
            print(f"=== {title} (gid {gid}) ===")
            for cf in cfs:
                print(json.dumps(cf, default=str))
        else:
            print(f"--- {title} (gid {gid}): no conditional formats")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <SPREADSHEET_ID>")
    main(sys.argv[1])
