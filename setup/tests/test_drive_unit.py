"""Offline regression tests for Drive safety and staged writes.

No Google credentials or network requests are used.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from drive_safety import (
    safe_drive_download_name, sha256_file, timestamps_match,
    validate_drive_timestamp,
)
import server


class TimestampValidationTests(unittest.TestCase):
    def test_offsets_and_utc_are_accepted(self):
        for value in ("2026-09-15T10:30:00+08:00",
                      "2026-09-15T02:30:00Z",
                      "2026-09-15T02:30:00.123456Z"):
            self.assertEqual(validate_drive_timestamp(value), value)

    def test_reject_naive_invalid_and_non_rfc_dates(self):
        for value in ("2026-09-15", "2026-09-15T10:30:00",
                      "2026-02-30T10:30:00Z",
                      "2026-W38-2T10:30:00Z",
                      "2026-09-15T10:30Z",
                      "2026-09-15T10:30:00+08:00 "):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_drive_timestamp(value)

    def test_timestamp_verification_compares_instants(self):
        self.assertTrue(timestamps_match(
            "2026-09-15T10:30:00+08:00", "2026-09-15T02:30:00Z"))
        self.assertFalse(timestamps_match(
            "2026-09-15T10:30:00+08:00", "2026-09-15T02:30:01Z"))
        self.assertFalse(timestamps_match("2026-09-15T02:30:00Z", ""))


class FileSafetyTests(unittest.TestCase):
    def test_download_name_blocks_traversal_and_reserved_names(self):
        self.assertEqual(safe_drive_download_name("..", "abc"), "abc-download")
        self.assertEqual(safe_drive_download_name("CON", "abc"), "abc-download")
        self.assertEqual(safe_drive_download_name("../secret.txt", "abc"), "abc-.._secret.txt")
        self.assertNotIn("/", safe_drive_download_name("a/b", "abc"))
        self.assertNotIn("\\", safe_drive_download_name("a\\b", "abc"))

    def test_hash_file_is_streaming(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.bin"
            path.write_bytes(b"x" * (4 * 1024 * 1024))
            with patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")):
                self.assertEqual(len(sha256_file(path)), 64)


class StagedDriveTests(unittest.TestCase):
    def setUp(self):
        server._STAGED.clear()
        self.audit = patch.object(server, "_audit_event")
        self.audit.start()

    def tearDown(self):
        server._STAGED.clear()
        self.audit.stop()

    def test_public_link_sharing_is_explicit_opt_in(self):
        with self.assertRaisesRegex(ValueError, "anyone_with_link_acknowledged"):
            server.google_drive_share("target")
        with self.assertRaisesRegex(ValueError, "anyone_with_link_acknowledged"):
            server.google_drive_share("target", role="writer")

    def test_upload_is_staged_and_preserves_requested_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.pdf"
            path.write_bytes(b"%PDF-example")
            d = MagicMock()
            req = d.files.return_value.create.return_value
            req.next_chunk.return_value = (None, {
                "id": "file1", "createdTime": "2026-09-15T02:30:00Z",
                "modifiedTime": "2026-09-15T02:30:00Z",
            })
            with patch.object(server, "_svc", return_value=d):
                staged = server.google_drive_upload(
                    str(path),
                    created_time="2026-09-15T10:30:00+08:00",
                    modified_time="2026-09-15T10:30:00+08:00",
                )
                self.assertTrue(staged["staged"])
                d.files.return_value.create.assert_not_called()
                applied = server.google_write_commit(staged["operation_id"])
            self.assertTrue(applied["applied"])
            kwargs = d.files.return_value.create.call_args.kwargs
            self.assertEqual(kwargs["body"]["createdTime"], "2026-09-15T10:30:00+08:00")
            self.assertEqual(kwargs["body"]["modifiedTime"], "2026-09-15T10:30:00+08:00")
            self.assertTrue(applied["result"]["timestamp_verification"]["createdTime"]["matches"])
            self.assertTrue(applied["result"]["timestamp_verification"]["modifiedTime"]["matches"])

    def test_upload_rejects_local_changes_after_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "draft.txt"
            path.write_text("before", encoding="utf-8")
            with patch.object(server, "_svc", return_value=MagicMock()) as service:
                staged = server.google_drive_upload(str(path))
                path.write_text("after", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "changed after staging"):
                    server.google_write_commit(staged["operation_id"])
                service.return_value.files.return_value.create.assert_not_called()

    def test_update_detects_stale_metadata(self):
        old = {"id": "a", "name": "old", "trashed": False,
               "version": "1", "modifiedTime": "2026-01-01T00:00:00Z",
               "parents": ["p"]}
        new = {**old, "version": "2", "name": "new"}
        with patch.object(server, "_drive_file", side_effect=[old, new]):
            staged = server.google_drive_update("a", name="renamed")
            with self.assertRaisesRegex(RuntimeError, "changed since staging"):
                server.google_write_commit(staged["operation_id"])

    def test_invalid_parent_is_not_a_folder(self):
        d = MagicMock()
        with patch.object(server, "_svc", return_value=d), patch.object(
                server, "_exec", return_value={
                    "id": "not-folder", "mimeType": "application/pdf", "trashed": False,
                }):
            with self.assertRaisesRegex(RuntimeError, "not an active folder"):
                server._drive_parent("not-folder")

    def test_inline_creation_is_staged_and_has_timestamp_fields(self):
        d = MagicMock()
        req = d.files.return_value.create.return_value
        req.next_chunk.return_value = (None, {
            "id": "created", "createdTime": "2026-09-15T02:30:00Z",
        })
        with patch.object(server, "_svc", return_value=d):
            staged = server.google_drive_create_file(
                "notes.txt", "hello", created_time="2026-09-15T10:30:00+08:00")
            d.files.return_value.create.assert_not_called()
            result = server.google_write_commit(staged["operation_id"])
        self.assertEqual(result["result"]["id"], "created")
        self.assertTrue(result["result"]["timestamp_verification"]["createdTime"]["matches"])


if __name__ == "__main__":
    unittest.main()
