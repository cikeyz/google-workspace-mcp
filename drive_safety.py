"""Pure helpers for safer Google Drive file operations.

Kept independent of Google/MCP dependencies for fast offline regression tests.
"""
from __future__ import annotations

import codecs
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

_RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,9})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z"
)
_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS = re.compile(
    r"(?i)^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$"
)


def validate_drive_timestamp(value: str, field: str = "timestamp") -> str:
    """Validate a timezone-aware RFC 3339 value; never guess a timezone."""
    if value == "":
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


def _timestamp_nanoseconds(value: str) -> int:
    """Preserve nanosecond precision that datetime.fromisoformat truncates."""
    validated = validate_drive_timestamp(value)
    if not validated:
        raise ValueError("timestamp must be nonempty.")
    parsed = datetime.fromisoformat(validated.replace("Z", "+00:00"))
    seconds = parsed.replace(microsecond=0).astimezone(timezone.utc)
    delta = seconds - datetime(1970, 1, 1, tzinfo=timezone.utc)
    fraction = validated.split(".", 1)[1].split("Z")[0] if "." in validated else ""
    # A fractional part may end in a numeric timezone offset.
    fraction = fraction.split("+", 1)[0].split("-", 1)[0]
    nanos = int(fraction.ljust(9, "0")) if fraction else 0
    return ((delta.days * 86400 + delta.seconds) * 1_000_000_000) + nanos


def timestamps_match(expected: str, actual: str) -> bool:
    """Compare absolute instants, including all 9 RFC 3339 fractional digits."""
    if not expected or not actual:
        return False
    try:
        return _timestamp_nanoseconds(expected) == _timestamp_nanoseconds(actual)
    except ValueError:
        return False

def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Hash a local file without loading the complete file into memory."""
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive.")
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_drive_download_name(name: str, file_id: str) -> str:
    """Sanitize a local leaf name, including its UTF-8 byte length.

    Reserve filename space for export extensions and temporary-file suffixes.
    """
    clean_id = re.sub(r"[^a-zA-Z0-9_-]", "_", file_id)[:64] or "drive-file"
    leaf = _UNSAFE_FILENAME.sub("_", name).strip(" .")
    if not leaf or _RESERVED_WINDOWS.match(leaf):
        leaf = "download"

    def shorten(text: str, max_bytes: int) -> str:
        return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")

    budget = 196 - len(clean_id.encode("ascii")) - 1
    if len(leaf.encode("utf-8")) > budget:
        stem, separator, extension = leaf.rpartition(".")
        ext = "." + extension if separator and stem and 1 <= len(extension.encode("utf-8")) <= 16 else ""
        leaf = shorten(stem if ext else leaf, budget - len(ext.encode("utf-8"))) + ext
    return f"{clean_id}-{leaf}"


class Utf8TextWindow:
    """Bounded-memory UTF-8 character window for Drive media downloads.

    Incremental decoding prevents multibyte characters crossing media chunks
    from being treated as replacement characters. Capture one extra character
    to decide if another page is needed.
    """

    def __init__(self, start_char: int, max_chars: int):
        if start_char < 0 or max_chars < 1:
            raise ValueError("Invalid text window.")
        self.start_char = start_char
        self.max_chars = max_chars
        self.chars_seen = 0
        self._stop = start_char + max_chars + 1
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._parts: list[str] = []
        self._finished = False

    def _accept(self, value: str) -> None:
        left = max(0, self.start_char - self.chars_seen)
        right = max(0, min(len(value), self._stop - self.chars_seen))
        if left < right:
            self._parts.append(value[left:right])
        self.chars_seen += len(value)

    def write(self, data: bytes) -> int:
        if self._finished:
            raise ValueError("Window already completed.")
        self._accept(self._decoder.decode(data, final=False))
        return len(data)

    def finish(self) -> None:
        if not self._finished:
            self._accept(self._decoder.decode(b"", final=True))
            self._finished = True

    @property
    def enough_to_stop(self) -> bool:
        return self.chars_seen >= self._stop

    @property
    def text(self) -> str:
        return "".join(self._parts)[:self.max_chars]
