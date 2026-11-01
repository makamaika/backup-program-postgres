"""No PostgreSQL connection or dump process is started by these tests."""

import threading
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, mock_open, patch

from pgbackup import backup, db
from pgbackup.agent.client import AgentClient
from pgbackup.errors import AgentError, BackupError, PgBackupError
from pgbackup import restore_lock


class ReplaceDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.profile = Mock()
        self.profile.display.return_value = "server"
        self.lock = threading.Lock()
        self.second_waiting = threading.Event()
        self.existing = False
        self.exists_checks = []
        self.drops = []
        self.renames = []
        self.patches = [
            patch.object(db, "connect", side_effect=AssertionError("unexpected DB connection")),
            patch.object(db, "replacement_lock", side_effect=self.replacement_lock),
            patch.object(db, "database_exists", side_effect=self.database_exists),
            patch.object(db, "create_database", side_effect=self.create),
            patch.object(db, "drop_database", side_effect=self.drop),
            patch.object(db, "rename_database", side_effect=self.rename),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def create(self, *args, **kwargs):
        if self.existing:
            raise RuntimeError("duplicate database")
        self.existing = True

    @contextmanager
    def replacement_lock(self):
        if threading.current_thread().name == "second":
            self.second_waiting.set()
        with self.lock:
            yield

    def database_exists(self, *args):
        self.exists_checks.append(threading.current_thread().name)
        return self.existing

    def drop(self, profile, name):
        self.drops.append(name)
        if name == "target":
            self.existing = False

    def rename(self, profile, old, new):
        self.renames.append((old, new))
        self.existing = new == "target"

    def test_concurrent_restore_sees_first_created_database(self):
        first_inside = threading.Event()
        release_first = threading.Event()
        errors = []

        def first():
            with db.replace_database(self.profile, "target"):
                first_inside.set()
                self.assertTrue(release_first.wait(2))

        def second():
            try:
                with db.replace_database(self.profile, "target"):
                    pass
            except PgBackupError as exc:
                errors.append(exc)

        a = threading.Thread(target=first)
        b = threading.Thread(target=second, name="second")
        a.start()
        self.assertTrue(first_inside.wait(2))
        b.start()
        self.assertTrue(self.second_waiting.wait(2))
        self.assertNotIn("second", self.exists_checks)
        self.assertEqual(self.drops, [])
        release_first.set()
        a.join(2)
        b.join(2)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertTrue(self.existing)
        self.assertEqual(self.drops, [])

    def test_create_failure_never_drops_unowned_target(self):
        def other_job_wins_create(*args, **kwargs):
            self.existing = True
            raise RuntimeError("duplicate database")

        with patch.object(db, "create_database", side_effect=other_job_wins_create):
            with self.assertRaisesRegex(RuntimeError, "duplicate database"):
                with db.replace_database(self.profile, "target"):
                    pass
        self.assertTrue(self.existing)
        self.assertEqual(self.drops, [])

    def test_restore_failure_rolls_back_created_database_and_old_one(self):
        self.existing = True
        with self.assertRaisesRegex(RuntimeError, "restore failed"):
            with db.replace_database(self.profile, "target", drop=True):
                raise RuntimeError("restore failed")
        self.assertEqual(self.drops, ["target"])
        self.assertEqual(self.renames[0][0], "target")
        self.assertEqual(self.renames[1][1], "target")
        self.assertTrue(self.existing)

    def test_success_drops_only_old_database(self):
        self.existing = True
        with db.replace_database(self.profile, "target", drop=True):
            pass
        self.assertEqual(self.drops, [self.renames[0][1]])
        self.assertTrue(self.existing)

    def test_maintenance_database_names_remain_valid_targets(self):
        for name in ("postgres", "template1"):
            self.existing = True
            with db.replace_database(self.profile, name, drop=True):
                pass


class SharedLockTests(unittest.TestCase):
    def test_local_sqlite_lock_blocks_second_job(self):
        temp_root = Path(__file__).resolve().parents[2]
        root = Path(tempfile.mkdtemp(prefix="pgbackup-lock-test-", dir=temp_root))
        entered = threading.Event()
        release = threading.Event()
        second_entered = threading.Event()
        with patch.object(restore_lock, "home_dir", return_value=root):
            def first():
                with restore_lock.replacement_lock():
                    entered.set()
                    self.assertTrue(release.wait(2))

            def second():
                with restore_lock.replacement_lock():
                    second_entered.set()

            a = threading.Thread(target=first)
            b = threading.Thread(target=second)
            a.start()
            self.assertTrue(entered.wait(2))
            b.start()
            self.assertFalse(second_entered.wait(0.1))
            release.set()
            a.join(2)
            b.join(2)
        self.assertTrue(second_entered.is_set())


class OutputReservationTests(unittest.TestCase):
    def setUp(self):
        self.profile = Mock(name="profile")
        self.profile.name = "profile"
        self.mkdir = patch.object(Path, "mkdir").start()
        self.addCleanup(patch.stopall)

    def test_same_second_and_safe_name_collisions_get_distinct_paths(self):
        other = Mock()
        other.name = "other"
        with patch.object(backup, "backups_dir", return_value=Path("backups")), \
             patch.object(backup, "datetime") as clock, \
             patch.object(Path, "is_dir", return_value=True), \
             patch.object(backup.os, "open", return_value=5) as open_mock, \
             patch.object(backup.os, "close"):
            clock.now.return_value = datetime(2026, 11, 1, tzinfo=timezone.utc)
            paths = [backup._reserve_out_path(Path("shared/"), self.profile, db, 1, "plain")
                     for db in ("a/b", "a:b")]
            paths.append(backup._reserve_out_path(Path("shared/"), other, "a/b", 1, "plain"))
            default_paths = [backup._reserve_out_path(None, self.profile, db, 1, "plain")
                             for db in ("a/b", "a:b")]
        self.assertEqual(len(set(paths + default_paths)), 5)
        self.assertEqual(open_mock.call_count, 5)
        for call in open_mock.call_args_list:
            self.assertTrue(call.args[1] & backup.os.O_EXCL)

    def test_existing_explicit_path_is_never_owned_or_removed(self):
        catalog = Mock()
        catalog.reserve_version.return_value = {"id": 1, "version_no": 1}
        with patch.object(Path, "exists", return_value=False), \
             patch.object(backup.os, "open", side_effect=FileExistsError), \
             patch.object(Path, "unlink") as unlink:
            with self.assertRaises(BackupError):
                backup.backup_to_file(catalog, self.profile, "db", out=Path("owned.sql"))
        unlink.assert_not_called()
        catalog.abort_backup.assert_called_once_with(1)

    def test_agent_pull_cleans_only_reserved_output(self):
        catalog = Mock()
        catalog.reserve_version.return_value = {"id": 1, "version_no": 1}
        agent = Mock()
        agent.name = "agent"
        with patch.object(backup, "_reserve_out_path", return_value=Path("reserved.dump")), \
             patch.object(backup, "AgentClient") as client, \
             patch.object(Path, "unlink") as unlink:
            client.return_value.download_database.side_effect = RuntimeError("download failed")
            with self.assertRaisesRegex(RuntimeError, "download failed"):
                backup.backup_agent_database_to_file(catalog, agent, "remote", "db")
        self.assertTrue(client.return_value.download_database.call_args.kwargs["replace_reserved"])
        unlink.assert_called_once_with(missing_ok=True)

    def test_agent_download_accepts_only_owned_reserved_destination(self):
        response = BytesIO(b"dump")
        response.headers = {}
        destination = Path("reserved.dump")
        with patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "mkdir"), \
             patch("builtins.open", mock_open()) as opened, \
             patch.object(backup.os, "replace") as replace, \
             patch.object(Path, "unlink"):
            with self.assertRaises(AgentError):
                AgentClient._download_response(object(), response, destination)
            opened.assert_not_called()
            size, digest = AgentClient._download_response(
                object(), response, destination, replace_reserved=True
            )
        self.assertEqual(size, 4)
        self.assertEqual(len(digest), 64)
        replace.assert_called_once()


if __name__ == "__main__":
    unittest.main()
