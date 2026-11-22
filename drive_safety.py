"""Pure helpers for safer Google Drive file operations.

Kept independent of Google/MCP dependencies for fast offline regression tests.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

_RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})\Z"
)
_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS = re.compile(
    r"(?i)^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$"
)


def validate_drive_timestamp(value: str, field: str = "timestamp") -> str:
    """Validate a timezone-aware RFC 3339 value; do not silently guess a timezone."""
    if not value:
        return ""
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        raise ValueError(
            f"{field} must be RFC 3339 with an explicit offset, "
            "for example 2026-09-15T10:30:00+08:00 or ...Z."
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} has an invalid calendar date or time.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset.")
    return value


def timestamps_match(expected: str, actual: str) -> bool:
    """Check instants, not textual representations (Google may normalize to UTC)."""
    if not expected or not actual:
        return False
    try:
        a = datetime.fromisoformat(validate_drive_timestamp(expected).replace("Z", "+00:00"))
        b = datetime.fromisoformat(validate_drive_timestamp(actual).replace("Z", "+00:00"))
    except ValueError:
        return False
    return a.astimezone(timezone.utc) == b.astimezone(timezone.utc)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Hash a local file without loading the complete file into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_drive_download_name(name: str, file_id: str) -> str:
    """Return a safe cross-platform leaf name; include the file ID to avoid collisions."""
    clean_id = re.sub(r"[^a-zA-Z0-9_-]", "_", file_id)[:120] or "drive-file"
    leaf = _UNSAFE_FILENAME.sub("_", name).strip(" .")[:180]
    if not leaf or _RESERVED_WINDOWS.match(leaf):
        leaf = "download"
    return f"{clean_id}-{leaf}"
