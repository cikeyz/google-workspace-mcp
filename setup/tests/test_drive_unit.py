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
        self.assertEqual(safe_drive_download_name("../secret.txt", "abc"), "abc-_secret.txt")
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
            with patch.object(server, "_svc", return_value=d), \
                    patch.object(server, "_drive_allocate_file_id", return_value="allocated"):
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
            with patch.object(server, "_svc", return_value=MagicMock()) as service, \
                    patch.object(server, "_drive_allocate_file_id", return_value="allocated"):
                staged = server.google_drive_upload(str(path))
                path.write_text("after", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "changed after staging"):
                    server.google_write_commit(staged["operation_id"])
                service.return_value.files.return_value.create.assert_not_called()

    def test_update_detects_stale_metadata(self):
        old = {"id": "a", "name": "old", "trashed": False,
               "version": "1", "modifiedTime": "2026-01-01T00:00:00Z",
               "parents": ["p"], "capabilities": {"canEdit": True}}
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




class AdditionalDriveRegressionTests(unittest.TestCase):
    def setUp(self):
        server._STAGED.clear()
        self.audit = patch.object(server, "_audit_event")
        self.audit.start()

    def tearDown(self):
        server._STAGED.clear()
        self.audit.stop()

    def test_nanosecond_timestamp_comparison_is_exact(self):
        self.assertFalse(timestamps_match(
            "2026-09-15T02:30:00.123456789Z",
            "2026-09-15T02:30:00.123456788Z"))
        self.assertTrue(timestamps_match(
            "2026-09-15T10:30:00.123456789+08:00",
            "2026-09-15T02:30:00.123456789Z"))

    def test_timestamp_rejects_null_value(self):
        with self.assertRaises(ValueError):
            validate_drive_timestamp(None)

    def test_download_leaf_limits_and_extension(self):
        name = safe_drive_download_name("x" * 350 + ".pdf", "id" * 60)
        self.assertLessEqual(len(name), 196)
        self.assertTrue(name.endswith(".pdf"))

    def test_hash_rejects_zero_chunk_size(self):
        with self.assertRaises(ValueError):
            sha256_file(Path("does-not-matter"), chunk_size=0)

    def test_capabilities_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "denies canShare"):
            server._drive_live_file({"trashed": False}, "canShare")
        with patch.object(server, "_svc", return_value=MagicMock()), patch.object(
                server, "_exec", return_value={
                    "id": "folder", "mimeType": "application/vnd.google-apps.folder",
                    "trashed": False, "capabilities": {},
                }):
            with self.assertRaisesRegex(RuntimeError, "No permission"):
                server._drive_parent("folder")

    def test_drive_search_sets_shared_drive_corpus_and_exposes_incomplete(self):
        service = MagicMock()
        with patch.object(server, "_svc", return_value=service), patch.object(
                server, "_exec", return_value={
                    "files": [{"id": "f"}], "incompleteSearch": True, "nextPageToken": "n",
                }):
            result = server.google_drive_search("name contains 'invoice'",
                                                max_results=40, drive_id="d1")
        params = service.files.return_value.list.call_args.kwargs
        self.assertEqual(params["driveId"], "d1")
        self.assertEqual(params["corpora"], "drive")
        self.assertTrue(params["supportsAllDrives"])
        self.assertTrue(result["incomplete_search"])
        self.assertEqual(result["next_page_token"], "n")

    def test_invalid_drive_page_sizes_are_rejected(self):
        for n in (0, -1):
            with self.subTest(n=n), self.assertRaises(ValueError):
                server._drive_page_size(n)

    def test_share_pages_permissions_without_refusing(self):
        service = MagicMock()
        file_meta = {"id": "x", "trashed": False, "capabilities": {"canShare": True}}
        responses = [
            {"permissions": [{"id": "b"}], "nextPageToken": "n"},
            {"permissions": [{"id": "a"}]},
        ]
        with patch.object(server, "_svc", return_value=service), patch.object(
                server, "_drive_file", return_value=file_meta), patch.object(
                server, "_exec", side_effect=responses):
            staged = server.google_drive_share("x", email="user@example.com")
        self.assertEqual([p["id"] for p in staged["preview"]["current_permissions"]],
                         ["a", "b"])
        self.assertEqual(service.permissions.return_value.list.call_count, 2)

    def test_move_without_remove_parents_infers_current_parent(self):
        file_meta = {
            "id": "x", "name": "x.txt", "trashed": False, "version": "5",
            "modifiedTime": "2026-11-01T12:00:00Z", "parents": ["old"],
            "capabilities": {"canEdit": True},
        }
        with patch.object(server, "_drive_parent", return_value={
                "id": "new", "mimeType": "application/vnd.google-apps.folder"
        }), patch.object(server, "_drive_file", return_value=file_meta):
            staged = server.google_drive_update("x", add_parents="new")
        self.assertEqual(staged["preview"]["remove_parents"], ["old"])
        self.assertEqual(staged["preview"]["add_parents"], ["new"])

    def test_move_rejects_existing_parent(self):
        file_meta = {
            "id": "x", "name": "x.txt", "trashed": False, "version": "5",
            "modifiedTime": "2026-11-01T12:00:00Z", "parents": ["current"],
            "capabilities": {"canEdit": True},
        }
        with patch.object(server, "_drive_parent", return_value={"id": "current"}), \
                patch.object(server, "_drive_file", return_value=file_meta):
            with self.assertRaisesRegex(ValueError, "Destination already"):
                server.google_drive_update("x", add_parents="current")

    def test_copy_rejects_folders(self):
        file_meta = {
            "id": "x", "trashed": False,
            "mimeType": "application/vnd.google-apps.folder",
            "capabilities": {"canCopy": True},
        }
        with patch.object(server, "_drive_file", return_value=file_meta):
            with self.assertRaisesRegex(ValueError, "does not copy folders"):
                server.google_drive_copy("x")

    def test_failed_download_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            file_meta = {
                "id": "x", "name": "file.bin", "mimeType": "application/octet-stream",
                "trashed": False, "capabilities": {"canDownload": True},
            }
            downloader = MagicMock()
            downloader.return_value.next_chunk.side_effect = OSError("simulated network error")
            with patch.object(server, "_drive_file", return_value=file_meta), \
                    patch.object(server, "_svc", return_value=MagicMock()), \
                    patch.object(server, "MediaIoBaseDownload", downloader), \
                    patch.object(server, "DOWNLOAD_DIR", Path(tmp)):
                with self.assertRaisesRegex(OSError, "simulated network"):
                    server.google_drive_download("x")
            self.assertEqual(list(Path(tmp).iterdir()), [])


class UnicodeAndDownloadTests(unittest.TestCase):
    def test_reject_malformed_timezone_minutes(self):
        for ts in ("2026-09-15T10:30:00+08:60",
                   "2026-09-15T10:30:00-25:00",
                   "2026-09-15T10:30:00+08:99"):
            with self.subTest(ts=ts), self.assertRaises(ValueError):
                validate_drive_timestamp(ts)

    def test_utf8_byte_length_is_bounded(self):
        file_name = safe_drive_download_name("猫" * 200 + ".pdf", "x" * 64)
        self.assertLessEqual(len(file_name.encode("utf-8")), 196)
        self.assertTrue(file_name.endswith(".pdf"))

    def test_unicode_window_survives_split_multibyte_characters(self):
        from drive_safety import Utf8TextWindow
        sink = Utf8TextWindow(3, 2)
        raw = "αβγ🐈猫XYZ".encode("utf-8")
        for part in (raw[:4], raw[4:7], raw[7:11], raw[11:]):
            sink.write(part)
        sink.finish()
        self.assertEqual(sink.text, "🐈猫")
        self.assertTrue(sink.enough_to_stop)
        self.assertEqual(sink.chars_seen, 8)

    def test_unicode_window_beyond_eof(self):
        from drive_safety import Utf8TextWindow
        sink = Utf8TextWindow(50, 10)
        sink.write("猫".encode("utf-8"))
        sink.finish()
        self.assertEqual(sink.text, "")
        self.assertEqual(sink.chars_seen, 1)
        self.assertFalse(sink.enough_to_stop)

    def test_drive_read_content_pages_by_characters(self):
        content = ("猫🐈" * 80).encode("utf-8")
        class DownloadStub:
            def __init__(self, stream, request, chunksize):
                self.stream = stream
                self.offset = 0

            def next_chunk(self, num_retries=0):
                chunk = content[self.offset:self.offset + 9]
                self.offset += len(chunk)
                self.stream.write(chunk)
                return None, self.offset >= len(content)

        metadata = {"id": "file", "name": "unicode.txt",
                    "mimeType": "text/plain", "trashed": False}
        with patch.object(server, "_drive_file", return_value=metadata), \
                patch.object(server, "_svc", return_value=MagicMock()), \
                patch.object(server, "MediaIoBaseDownload", DownloadStub):
            result = server.google_drive_read_content("file", max_chars=13, start_char=37)
        expected = ("猫🐈" * 80)[37:50]
        self.assertEqual(result["text"], expected)
        self.assertTrue(result["text_truncated"])
        self.assertFalse(result["text_chars_exact"])
        self.assertEqual(result["next_start_char"], 50)

    def test_download_does_not_overwrite_existing_local_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = {"id": "id1", "name": "report.txt", "mimeType": "text/plain",
                        "trashed": False, "capabilities": {"canDownload": True}}
            expected = Path(tmp) / safe_drive_download_name("report.txt", "id1")
            expected.write_text("existing user edits", encoding="utf-8")
            class DownloadStub:
                def __init__(self, stream, request, chunksize):
                    self.stream = stream
                def next_chunk(self, num_retries=0):
                    self.stream.write(b"new download content")
                    return None, True
            with patch.object(server, "_drive_file", return_value=metadata), \
                    patch.object(server, "_svc", return_value=MagicMock()), \
                    patch.object(server, "MediaIoBaseDownload", DownloadStub), \
                    patch.object(server, "DOWNLOAD_DIR", Path(tmp)):
                result = server.google_drive_download("id1")
            self.assertEqual(expected.read_text(encoding="utf-8"), "existing user edits")
            self.assertNotEqual(result["path"], str(expected))
            self.assertEqual(Path(result["path"]).read_bytes(), b"new download content")


class ExpandedDriveApiTests(unittest.TestCase):
    def setUp(self):
        server._STAGED.clear()
        self.audit = patch.object(server, "_audit_event")
        self.audit.start()

    def tearDown(self):
        server._STAGED.clear()
        self.audit.stop()

    def test_tool_registration_includes_expanded_drive_surface(self):
        names = {t.name for t in server.mcp._tool_manager.list_tools()}
        expected = {
            "google_drive_about", "google_drive_shared_drives",
            "google_drive_changes_start", "google_drive_changes",
            "google_drive_revisions", "google_drive_comments",
            "google_drive_file_labels", "google_drive_restore",
            "google_drive_shortcut", "google_drive_permission_remove",
            "google_drive_metadata_update", "google_drive_comment_create",
            "google_drive_comment_reply",
        }
        self.assertEqual(len(names), 90)
        self.assertTrue(expected <= names)

    def test_about_uses_explicit_field_mask(self):
        with patch.object(server, "_svc", return_value=MagicMock()) as svc, \
                patch.object(server, "_exec", return_value={"canCreateDrives": True}):
            self.assertTrue(server.google_drive_about()["canCreateDrives"])
        args = svc.return_value.about.return_value.get.call_args.kwargs
        self.assertIn("storageQuota", args["fields"])

    def test_shared_drive_list_has_cursor(self):
        svc = MagicMock()
        with patch.object(server, "_svc", return_value=svc), \
                patch.object(server, "_exec", return_value={
                    "drives": [{"id": "d1"}], "nextPageToken": "next",
                }):
            response = server.google_drive_shared_drives(max_results=500)
        self.assertTrue(response["has_more"])
        self.assertEqual(svc.drives.return_value.list.call_args.kwargs["pageSize"], 100)

    def test_changes_cursor_uses_shared_drive_id(self):
        svc = MagicMock()
        with patch.object(server, "_svc", return_value=svc), \
                patch.object(server, "_exec", return_value={"startPageToken": "t"}):
            result = server.google_drive_changes_start(drive_id="d1")
        self.assertEqual(result["start_page_token"], "t")
        self.assertEqual(svc.changes.return_value.getStartPageToken.call_args.kwargs["driveId"], "d1")

    def test_changes_preserve_next_and_new_start_tokens(self):
        svc = MagicMock()
        with patch.object(server, "_svc", return_value=svc), \
                patch.object(server, "_exec", return_value={
                    "changes": [{"fileId": "f"}],
                    "newStartPageToken": "future",
                }):
            result = server.google_drive_changes("old", drive_id="d1")
        self.assertEqual(result["new_start_page_token"], "future")
        self.assertFalse(result["has_more"])
        self.assertEqual(svc.changes.return_value.list.call_args.kwargs["pageToken"], "old")

    def test_changes_require_cursor(self):
        with self.assertRaisesRegex(ValueError, "page_token is required"):
            server.google_drive_changes("")

    def test_revisions_capability_check(self):
        with patch.object(server, "_drive_file", return_value={
                    "id": "f", "trashed": False,
                    "capabilities": {"canReadRevisions": False},
                }):
            with self.assertRaisesRegex(RuntimeError, "canReadRevisions"):
                server.google_drive_revisions("f")

    def test_revision_list_cursor(self):
        svc = MagicMock()
        with patch.object(server, "_drive_file", return_value={
                    "id": "f", "trashed": False,
                    "capabilities": {"canReadRevisions": True},
                }), patch.object(server, "_svc", return_value=svc), \
                patch.object(server, "_exec", return_value={"revisions": [{"id": "r1"}]}):
            result = server.google_drive_revisions("f")
        self.assertEqual(result["items"][0]["id"], "r1")
        self.assertIn("revisions(", svc.revisions.return_value.list.call_args.kwargs["fields"])

    def test_comments_list_sends_fields_parameter(self):
        svc = MagicMock()
        with patch.object(server, "_svc", return_value=svc), \
                patch.object(server, "_exec", return_value={"comments": [{"id": "c"}]}):
            result = server.google_drive_comments("f")
        self.assertEqual(result["result_count"], 1)
        self.assertIn("comments(", svc.comments.return_value.list.call_args.kwargs["fields"])

    def test_file_labels_list_uses_max_results(self):
        svc = MagicMock()
        with patch.object(server, "_svc", return_value=svc), \
                patch.object(server, "_exec", return_value={"labels": [{"id": "lab"}]}):
            result = server.google_drive_file_labels("f")
        self.assertEqual(result["result_count"], 1)
        self.assertEqual(svc.files.return_value.listLabels.call_args.kwargs["maxResults"], 100)

    def test_restore_is_staged_and_uses_untrash_flag(self):
        meta = {"id": "f", "name": "x", "trashed": True, "version": "1",
                "modifiedTime": "t", "parents": [],
                "capabilities": {"canUntrash": True}}
        svc = MagicMock()
        with patch.object(server, "_drive_file", return_value=meta), \
                patch.object(server, "_svc", return_value=svc), \
                patch.object(server, "_exec", return_value={"id": "f", "trashed": False}):
            staged = server.google_drive_restore("f")
            svc.files.return_value.update.assert_not_called()
            done = server.google_write_commit(staged["operation_id"])
        self.assertTrue(done["result"]["trashed"] is False)
        self.assertIs(svc.files.return_value.update.call_args.kwargs["body"]["trashed"], False)

    def test_restore_denies_untrash_without_capability(self):
        with patch.object(server, "_drive_file", return_value={
                "id": "f", "trashed": True, "capabilities": {"canUntrash": False}}):
            with self.assertRaisesRegex(RuntimeError, "canUntrash"):
                server.google_drive_restore("f")

    def test_shortcut_is_staged_and_only_targets_existing_item(self):
        meta = {"id": "target", "name": "Document", "mimeType": "text/plain",
                "trashed": False, "version": "1", "modifiedTime": "x",
                "parents": []}
        svc = MagicMock()
        with patch.object(server, "_drive_file", return_value=meta), \
                patch.object(server, "_drive_parent", return_value=None), \
                patch.object(server, "_svc", return_value=svc), \
                patch.object(server, "_drive_commit_once", return_value={"id": "shortcut"}):
            staged = server.google_drive_shortcut("target")
            svc.files.return_value.create.assert_not_called()
            result = server.google_write_commit(staged["operation_id"])
        self.assertEqual(result["result"]["id"], "shortcut")
        body = svc.files.return_value.create.call_args.kwargs["body"]
        self.assertEqual(body["shortcutDetails"]["targetId"], "target")

    def test_permission_remove_needs_acknowledgement(self):
        with self.assertRaisesRegex(ValueError, "removal_acknowledged"):
            server.google_drive_permission_remove("f", "p")

    def test_permission_remove_protects_owner(self):
        meta = {"id": "f", "trashed": False,
                "capabilities": {"canShare": True}}
        with patch.object(server, "_drive_file", return_value=meta), \
                patch.object(server, "google_drive_permissions", return_value={
                    "permissions": [{"id": "p", "role": "owner"}]}):
            with self.assertRaisesRegex(RuntimeError, "privileged"):
                server.google_drive_permission_remove("f", "p", True)

    def test_permission_remove_commit_is_explicit(self):
        meta = {"id": "f", "name": "f", "trashed": False,
                "version": "1", "modifiedTime": "x", "parents": [],
                "capabilities": {"canShare": True}}
        svc = MagicMock()
        with patch.object(server, "_drive_file", return_value=meta), \
                patch.object(server, "google_drive_permissions", return_value={
                    "permissions": [{"id": "p", "role": "reader"}]}), \
                patch.object(server, "_svc", return_value=svc), \
                patch.object(server, "_drive_commit_once", return_value=None):
            stage = server.google_drive_permission_remove("f", "p", True)
            svc.permissions.return_value.delete.assert_not_called()
            result = server.google_write_commit(stage["operation_id"])
        self.assertTrue(result["result"]["removed"])
        self.assertEqual(svc.permissions.return_value.delete.call_args.kwargs["permissionId"], "p")

    def test_metadata_patch_is_allowlisted_and_staged(self):
        meta = {"id": "f", "name": "f", "trashed": False,
                "version": "1", "modifiedTime": "x", "parents": [],
                "capabilities": {"canEdit": True}}
        svc = MagicMock()
        with patch.object(server, "_drive_file", return_value=meta), \
                patch.object(server, "_svc", return_value=svc), \
                patch.object(server, "_exec", return_value={"id": "f"}):
            st = server.google_drive_metadata_update(
                "f", description="", starred="false",
                properties_json='{"project":"talas","legacy":null}')
            svc.files.return_value.update.assert_not_called()
            server.google_write_commit(st["operation_id"])
        body = svc.files.return_value.update.call_args.kwargs["body"]
        self.assertEqual(body["description"], "")
        self.assertIs(body["starred"], False)
        self.assertIsNone(body["properties"]["legacy"])

    def test_metadata_rejects_malformed_properties(self):
        with self.assertRaisesRegex(ValueError, "JSON object"):
            server.google_drive_metadata_update("f", properties_json='[1,2]')

    def test_comment_create_is_staged(self):
        meta = {"id": "f", "name": "f", "trashed": False,
                "version": "1", "modifiedTime": "x", "parents": [],
                "capabilities": {"canComment": True}}
        svc = MagicMock()
        with patch.object(server, "_drive_file", return_value=meta), \
                patch.object(server, "_svc", return_value=svc), \
                patch.object(server, "_drive_commit_once", return_value={"id": "c"}):
            stage = server.google_drive_comment_create("f", "Please review")
            svc.comments.return_value.create.assert_not_called()
            result = server.google_write_commit(stage["operation_id"])
        self.assertEqual(result["result"]["id"], "c")
        self.assertIn("fields", svc.comments.return_value.create.call_args.kwargs)

    def test_comment_reply_revalidates_parent_comment(self):
        meta = {"id": "f", "trashed": False,
                "capabilities": {"canComment": True}}
        svc = MagicMock()
        states = [{"id": "c", "modifiedTime": "1", "deleted": False},
                  {"id": "c", "modifiedTime": "2", "deleted": False}]
        with patch.object(server, "_drive_file", return_value=meta), \
                patch.object(server, "_svc", return_value=svc), \
                patch.object(server, "_exec", side_effect=states), \
                patch.object(server, "_drive_commit_once", return_value={"id": "reply"}):
            st = server.google_drive_comment_reply("f", "c", "Thanks")
            with self.assertRaisesRegex(RuntimeError, "changed since staging"):
                server.google_write_commit(st["operation_id"])
            svc.replies.return_value.create.assert_not_called()



class UploadIntegrityTests(unittest.TestCase):
    def setUp(self):
        server._STAGED.clear()
        self.audit = patch.object(server, "_audit_event")
        self.audit.start()

    def tearDown(self):
        server._STAGED.clear()
        self.audit.stop()

    def test_preallocated_id_uses_drive_generate_ids(self):
        service = MagicMock()
        with patch.object(server, "_svc", return_value=service), \
                patch.object(server, "_exec", return_value={"ids": ["my-id"]}):
            value = server._drive_allocate_file_id()
        self.assertEqual(value, "my-id")
        args = service.files.return_value.generateIds.call_args.kwargs
        self.assertEqual(args["count"], 1)
        self.assertEqual(args["space"], "drive")

    def test_upload_commit_uses_reserved_id_and_verifies_sha256(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            file_path = Path(folder) / "sample.txt"
            file_path.write_bytes(b"content")
            digest = hashlib.sha256(b"content").hexdigest()
            service = MagicMock()
            service.files.return_value.create.return_value.next_chunk.return_value = (
                None, {"id": "my-id", "sha256Checksum": digest})
            with patch.object(server, "_svc", return_value=service), \
                    patch.object(server, "_drive_allocate_file_id", return_value="my-id"):
                stage = server.google_drive_upload(str(file_path))
                self.assertEqual(stage["preview"]["allocated_file_id"], "my-id")
                result = server.google_write_commit(stage["operation_id"])
            self.assertEqual(service.files.return_value.create.call_args.kwargs["body"]["id"], "my-id")
            self.assertTrue(result["result"]["sha256_verified"])

    def test_download_rejects_checksum_mismatch_without_publishing(self):
        metadata = {"id": "f", "name": "x.bin",
                    "mimeType": "application/octet-stream", "trashed": False,
                    "sha256Checksum": "0" * 64,
                    "capabilities": {"canDownload": True}}
        class DownloadStub:
            def __init__(self, sink, request, chunksize):
                self.sink = sink
            def next_chunk(self, num_retries=0):
                self.sink.write(b"corrupt bytes")
                return None, True
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(server, "_drive_file", return_value=metadata), \
                patch.object(server, "_svc", return_value=MagicMock()), \
                patch.object(server, "MediaIoBaseDownload", DownloadStub), \
                patch.object(server, "DOWNLOAD_DIR", Path(folder)):
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                server.google_drive_download("f")
            self.assertFalse(list(Path(folder).iterdir()))



class FinalDriveWorkflowTests(unittest.TestCase):
    def setUp(self):
        server._STAGED.clear()
        self.audit = patch.object(server, "_audit_event")
        self.audit.start()

    def tearDown(self):
        server._STAGED.clear()
        self.audit.stop()

    @staticmethod
    def _editable_binary(version="3"):
        return {
            "id": "f", "name": "original.pdf", "mimeType": "application/pdf",
            "size": "100", "trashed": False, "version": version,
            "modifiedTime": "2026-11-01T00:00:00Z",
            "parents": ["root"], "sha256Checksum": "old",
            "capabilities": {"canModifyContent": True},
        }

    def test_all_89_tools_are_registered(self):
        registered = {tool.name for tool in server.mcp._tool_manager.list_tools()}
        self.assertEqual(len(registered), 90)
        self.assertTrue({
            "google_drive_replace_content", "google_drive_revision_download",
            "google_drive_label_modify", "google_drive_revision_pin",
        } <= registered)

    def test_binary_replace_requires_acknowledgement(self):
        with self.assertRaisesRegex(ValueError, "replace_acknowledged"):
            server.google_drive_replace_content("f", "anything.pdf")

    def test_binary_replace_rejects_google_native(self):
        meta = {**self._editable_binary(), "mimeType": "application/vnd.google-apps.document"}
        with tempfile.TemporaryDirectory() as temp:
            file_path = Path(temp) / "content"
            file_path.write_bytes(b"abc")
            with patch.object(server, "_drive_file", return_value=meta):
                with self.assertRaisesRegex(ValueError, "existing binary"):
                    server.google_drive_replace_content(
                        "f", str(file_path), replace_acknowledged=True)

    def test_replace_is_staged_and_preserves_file_id(self):
        import hashlib
        digest = hashlib.sha256(b"new binary bytes").hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp) / "new.pdf"
            local.write_bytes(b"new binary bytes")
            svc = MagicMock()
            svc.files.return_value.update.return_value.next_chunk.return_value = (
                None, {"id": "f", "sha256Checksum": digest})
            with patch.object(server, "_drive_file", return_value=self._editable_binary()), \
                    patch.object(server, "_svc", return_value=svc):
                staged = server.google_drive_replace_content(
                    "f", str(local), replace_acknowledged=True,
                    keep_revision_forever=True)
                svc.files.return_value.update.assert_not_called()
                applied = server.google_write_commit(staged["operation_id"])
            params = svc.files.return_value.update.call_args.kwargs
            self.assertEqual(params["fileId"], "f")
            self.assertTrue(params["keepRevisionForever"])
            self.assertEqual(params["fields"], server._DRIVE_FIELDS)
            self.assertEqual(applied["result"]["replacement_file_id"], "f")
            self.assertTrue(applied["result"]["sha256_verified"])

    def test_replace_revalidates_file_and_local_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp) / "new.pdf"
            local.write_bytes(b"first")
            with patch.object(server, "_drive_file", return_value=self._editable_binary()), \
                    patch.object(server, "_svc", return_value=MagicMock()) as svc:
                staged = server.google_drive_replace_content("f", str(local), True)
                local.write_bytes(b"second")
                with self.assertRaisesRegex(RuntimeError, "changed since staging"):
                    server.google_write_commit(staged["operation_id"])
                svc.return_value.files.return_value.update.assert_not_called()

    def test_replace_refuses_stale_remote_version(self):
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp) / "new.pdf"
            local.write_bytes(b"unchanged")
            with patch.object(server, "_drive_file",
                              side_effect=[self._editable_binary(),
                                           self._editable_binary(version="4")]), \
                    patch.object(server, "_svc", return_value=MagicMock()) as svc:
                staged = server.google_drive_replace_content("f", str(local), True)
                with self.assertRaisesRegex(RuntimeError, "Drive file content or metadata changed"):
                    server.google_write_commit(staged["operation_id"])
                svc.return_value.files.return_value.update.assert_not_called()

    def test_revision_download_requires_keep_forever(self):
        file_meta = {
            "id": "f", "name": "x.pdf", "mimeType": "application/pdf",
            "trashed": False,
            "capabilities": {"canDownload": True, "canReadRevisions": True},
        }
        with patch.object(server, "_drive_file", return_value=file_meta), \
                patch.object(server, "_svc", return_value=MagicMock()), \
                patch.object(server, "_exec", return_value={"id": "rev", "keepForever": False}):
            with self.assertRaisesRegex(RuntimeError, "Keep Forever"):
                server.google_drive_revision_download("f", "rev")

    def test_revision_download_rejects_native_documents(self):
        meta = {"id": "f", "mimeType": "application/vnd.google-apps.document",
                "trashed": False,
                "capabilities": {"canDownload": True, "canReadRevisions": True}}
        with patch.object(server, "_drive_file", return_value=meta):
            with self.assertRaisesRegex(ValueError, "binary files"):
                server.google_drive_revision_download("f", "rev")

    def test_revision_download_unique_and_md5_verified(self):
        import hashlib
        data = b"old revision content"
        meta = {"id": "f", "name": "file.pdf", "mimeType": "application/pdf",
                "trashed": False,
                "capabilities": {"canDownload": True, "canReadRevisions": True}}
        revision = {"id": "r1", "keepForever": True, "originalFilename": "file.pdf",
                    "md5Checksum": hashlib.md5(data, usedforsecurity=False).hexdigest()}
        class Downloader:
            def __init__(self, stream, req, chunksize):
                self.stream = stream
            def next_chunk(self, num_retries=0):
                self.stream.write(data)
                return None, True
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(server, "_drive_file", return_value=meta), \
                patch.object(server, "_svc", return_value=MagicMock()) as svc, \
                patch.object(server, "_exec", return_value=revision), \
                patch.object(server, "MediaIoBaseDownload", Downloader), \
                patch.object(server, "DOWNLOAD_DIR", Path(temp)):
            first = server.google_drive_revision_download("f", "r1")
            second = server.google_drive_revision_download("f", "r1")
            self.assertNotEqual(first["path"], second["path"])
            self.assertEqual(Path(first["path"]).read_bytes(), data)
            self.assertEqual(Path(second["path"]).read_bytes(), data)
            self.assertTrue(first["md5_verified"])
            self.assertEqual(svc.return_value.revisions.return_value.get_media.call_count, 2)

    def test_revision_download_refuses_corrupt_bytes(self):
        meta = {"id": "f", "name": "x.bin", "mimeType": "application/octet-stream",
                "trashed": False,
                "capabilities": {"canDownload": True, "canReadRevisions": True}}
        revision = {"id": "r1", "keepForever": True, "md5Checksum": "0" * 32}
        class Downloader:
            def __init__(self, stream, req, chunksize):
                self.stream = stream
            def next_chunk(self, num_retries=0):
                self.stream.write(b"not the expected bytes")
                return None, True
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(server, "_drive_file", return_value=meta), \
                patch.object(server, "_svc", return_value=MagicMock()), \
                patch.object(server, "_exec", return_value=revision), \
                patch.object(server, "MediaIoBaseDownload", Downloader), \
                patch.object(server, "DOWNLOAD_DIR", Path(temp)):
            with self.assertRaisesRegex(RuntimeError, "MD5 checksum"):
                server.google_drive_revision_download("f", "r1")
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_label_field_validation(self):
        p = server._drive_label_field_modifications
        self.assertEqual(p('[{"fieldId":"t","setTextValues":["value"]}]'),
                         [{"fieldId": "t", "setTextValues": ["value"]}])
        self.assertEqual(p('[{"fieldId":"f","unsetValues":true}]'),
                         [{"fieldId": "f", "unsetValues": True}])
        with self.assertRaisesRegex(ValueError, "exactly one"):
            p('[{"fieldId":"f","setTextValues":["x"],"unsetValues":true}]')
        with self.assertRaisesRegex(ValueError, "64-bit"):
            p('[{"fieldId":"f","setIntegerValues":["999999999999999999999999999"]}]')
        with self.assertRaisesRegex(ValueError, "Invalid calendar date"):
            p('[{"fieldId":"f","setDateValues":["2026-99-99"]}]')
        with self.assertRaisesRegex(ValueError, "supported Drive keys"):
            p('[{"fieldId":"f","removeLabel":true}]')

    @staticmethod
    def _label_meta(value="a"):
        return {
            "id": "f", "name": "notes.pdf", "trashed": False, "version": "10",
            "modifiedTime": "2026-11-01T00:00:00Z", "parents": ["root"],
            "capabilities": {"canModifyLabels": True},
            "labelInfo": {"labels": [{"id": "L", "fields": {"F": {"value": value}}}]},
        }

    def test_label_modify_stages_typed_values(self):
        svc = MagicMock()
        svc.files.return_value.modifyLabels.return_value.execute.return_value = {
            "modifiedLabels": [{"id": "L"}]}
        with patch.object(server, "_drive_label_snapshot", return_value=self._label_meta()), \
                patch.object(server, "_svc", return_value=svc), \
                patch.object(server, "_drive_commit_once",
                             return_value={"modifiedLabels": [{"id": "L"}]}):
            stage = server.google_drive_label_modify(
                "f", "L", field_modifications_json=
                '[{"fieldId":"F","setTextValues":["reviewed"]}]')
            svc.files.return_value.modifyLabels.assert_not_called()
            result = server.google_write_commit(stage["operation_id"])
        self.assertEqual(result["result"]["modifiedLabels"][0]["id"], "L")
        body = svc.files.return_value.modifyLabels.call_args.kwargs["body"]
        self.assertEqual(body["labelModifications"][0]["fieldModifications"][0]["setTextValues"],
                         ["reviewed"])

    def test_label_removal_requires_no_field_modifications(self):
        with self.assertRaisesRegex(ValueError, "simultaneously"):
            server.google_drive_label_modify(
                "f", "L", remove_label=True,
                field_modifications_json='[{"fieldId":"f","unsetValues":true}]')

    def test_label_removal_sends_remove_label_true(self):
        svc = MagicMock()
        with patch.object(server, "_drive_label_snapshot", return_value=self._label_meta()), \
                patch.object(server, "_svc", return_value=svc), \
                patch.object(server, "_drive_commit_once",
                             return_value={"modifiedLabels": []}):
            stage = server.google_drive_label_modify("f", "L", remove_label=True)
            result = server.google_write_commit(stage["operation_id"])
        self.assertTrue(result["result"]["removed"])
        body = svc.files.return_value.modifyLabels.call_args.kwargs["body"]
        self.assertTrue(body["labelModifications"][0]["removeLabel"])

    def test_label_modify_detects_concurrent_label_change(self):
        svc = MagicMock()
        with patch.object(server, "_drive_label_snapshot", side_effect=[
                    self._label_meta("before"), self._label_meta("after")]), \
                patch.object(server, "_svc", return_value=svc):
            staged = server.google_drive_label_modify("f", "L")
            with self.assertRaisesRegex(RuntimeError, "selected Drive label changed"):
                server.google_write_commit(staged["operation_id"])
            svc.files.return_value.modifyLabels.assert_not_called()



class RevisionRetentionTests(unittest.TestCase):
    def setUp(self):
        server._STAGED.clear()
        self.audit = patch.object(server, "_audit_event")
        self.audit.start()

    def tearDown(self):
        server._STAGED.clear()
        self.audit.stop()

    @staticmethod
    def _file_meta():
        return {"id": "f", "name": "sample.pdf", "mimeType": "application/pdf",
                "trashed": False, "version": "3",
                "modifiedTime": "2026-11-01T00:00:00Z", "parents": ["folder"],
                "capabilities": {"canReadRevisions": True, "canModifyContent": True}}

    def test_pin_requires_retention_acknowledgement(self):
        with self.assertRaisesRegex(ValueError, "keep_forever_acknowledged"):
            server.google_drive_revision_pin("f", "old-rev")

    def test_pin_rejects_already_pinned_revision(self):
        svc = MagicMock()
        with patch.object(server, "_drive_file", return_value=self._file_meta()), \
                patch.object(server, "_svc", return_value=svc), \
                patch.object(server, "_exec", return_value={
                    "id": "r", "keepForever": True}):
            with self.assertRaisesRegex(ValueError, "already marked"):
                server.google_drive_revision_pin("f", "r", True)

    def test_pin_is_staged_and_updates_correct_revision(self):
        service = MagicMock()
        initial = {"id": "r", "keepForever": False, "md5Checksum": "abc"}
        with patch.object(server, "_drive_file", return_value=self._file_meta()), \
                patch.object(server, "_svc", return_value=service), \
                patch.object(server, "_exec", return_value=initial), \
                patch.object(server, "_drive_commit_once",
                             return_value={"id": "r", "keepForever": True}):
            st = server.google_drive_revision_pin("f", "r", True)
            service.revisions.return_value.update.assert_not_called()
            committed = server.google_write_commit(st["operation_id"])
        params = service.revisions.return_value.update.call_args.kwargs
        self.assertEqual(params["fileId"], "f")
        self.assertEqual(params["revisionId"], "r")
        self.assertIs(params["body"]["keepForever"], True)
        self.assertTrue(committed["result"]["keepForever"])

    def test_pin_detects_revision_change_before_commit(self):
        service = MagicMock()
        first = {"id": "r", "keepForever": False, "md5Checksum": "one"}
        second = {"id": "r", "keepForever": False, "md5Checksum": "two"}
        with patch.object(server, "_drive_file", return_value=self._file_meta()), \
                patch.object(server, "_svc", return_value=service), \
                patch.object(server, "_exec", side_effect=[first, second]):
            st = server.google_drive_revision_pin("f", "r", True)
            with self.assertRaisesRegex(RuntimeError, "Revision changed"):
                server.google_write_commit(st["operation_id"])
            service.revisions.return_value.update.assert_not_called()

if __name__ == "__main__":
    unittest.main()
