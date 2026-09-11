"""Lease, runtime ownership and lifecycle tests; never modify the real taskbar."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from taskbar_reservation import LEASE, WIDTH, ReservationClient


class ReservationTests(unittest.TestCase):
    def client(self, directory):
        client = ReservationClient.__new__(ReservationClient)
        client.api, client.kernel = Mock(), Mock()
        client.kernel.GetTickCount64.return_value = 123456
        client.directory = directory
        client.hwnd, client.worker = 0, None
        client.closed, client.error, client.next_start = False, "", 0
        client._running_executable = Mock(return_value=None)
        return client

    def test_lease_renews_and_window_recreation_releases_old_handle(self):
        c = self.client(Path("unused"))
        c.next_start = float("inf")
        c.request(100, 403)
        c.api.SetPropW.assert_any_call(100, WIDTH, 415)
        c.api.SetPropW.assert_any_call(100, LEASE, 123456)
        c.kernel.GetTickCount64.return_value += 250
        c.request(100, 403)
        c.api.SetPropW.assert_any_call(100, LEASE, 123706)
        c.request(200, 600)
        c.api.RemovePropW.assert_any_call(100, WIDTH)
        c.api.RemovePropW.assert_any_call(100, LEASE)
        c.release()
        c.api.RemovePropW.assert_any_call(200, WIDTH)
        c.api.RemovePropW.assert_any_call(200, LEASE)

    def test_close_stops_only_our_runtime_and_never_renews_again(self):
        c = self.client(Path("private-runtime").resolve())
        c.hwnd = 100
        c._running_executable.return_value = c.directory / "version/windhawk.exe"
        with patch("taskbar_reservation.subprocess.run") as run:
            c.close()
            run.assert_called_once()
        c.request(100, 400)
        c.api.SetPropW.assert_not_called()
        c._running_executable.return_value = Path("another-install/windhawk.exe").resolve()
        with patch("taskbar_reservation.subprocess.run") as run:
            c.close()
            run.assert_not_called()

    def test_existing_user_windhawk_is_neither_stopped_nor_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "runtime-id.txt").write_text("0123456789abcdef")
            (bundle / "windhawk.exe").write_bytes(b"test")
            c = self.client(root / "private")
            c._running_executable.return_value = root / "users-windhawk/windhawk.exe"
            with patch("taskbar_reservation.runtime_bundle", return_value=bundle), \
                    patch("taskbar_reservation.subprocess.Popen") as popen, \
                    patch("taskbar_reservation.subprocess.run") as run:
                c._start()
            self.assertIn("已有其他 Windhawk", c.error)
            popen.assert_not_called()
            run.assert_not_called()

    def test_upgrade_reuses_pdb_cache_without_copying_old_mod_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "runtime-id.txt").write_text("0123456789abcdef")
            c = self.client(root / "private")
            previous = c.directory / "old-version/AppData/Engine"
            pdb = previous / "Symbols/Taskbar.pdb/module-guid/Taskbar.pdb"
            pdb.parent.mkdir(parents=True)
            pdb.write_bytes(b"cached-pdb")
            state = previous / "ModsWritable/aifuel-taskbar-space.ini"
            state.parent.mkdir()
            state.write_text("old mod state must not migrate")
            with patch("taskbar_reservation.runtime_bundle", return_value=bundle), \
                    patch("taskbar_reservation.subprocess.Popen"):
                c._start()
            target = c.directory / "0123456789abcdef/AppData/Engine"
            self.assertEqual((target / pdb.relative_to(previous)).read_bytes(), b"cached-pdb")
            self.assertFalse((target / "ModsWritable").exists())

    def test_close_during_copy_does_not_launch_an_orphan_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "runtime-id.txt").write_text("0123456789abcdef")
            c = self.client(root / "private")

            def copy(source, target, **kwargs):
                target.mkdir(parents=True)
                c.close()

            with patch("taskbar_reservation.runtime_bundle", return_value=bundle), \
                    patch("taskbar_reservation.shutil.copytree", side_effect=copy), \
                    patch("taskbar_reservation.subprocess.Popen") as popen:
                c._start()
            popen.assert_not_called()
            self.assertTrue(c.closed)

    def test_launch_is_serialized_and_can_retry_after_failure(self):
        c = self.client(Path("unused"))
        c._start = Mock()

        class Deferred:
            def __init__(self, **kwargs):
                self.alive = False
            def start(self):
                self.alive = True
            def is_alive(self):
                return self.alive

        with patch("taskbar_reservation.threading.Thread", side_effect=Deferred) as thread, \
                patch("taskbar_reservation.time.monotonic", return_value=10):
            c.request(100, 400)
            c.request(100, 400)
            thread.assert_called_once()
            first = c.worker
        with patch("taskbar_reservation.threading.Thread", side_effect=Deferred) as thread, \
                patch("taskbar_reservation.time.monotonic", return_value=30):
            c.request(100, 400)
            thread.assert_not_called()
            first.alive = False
            c.request(100, 400)
            thread.assert_called_once()
