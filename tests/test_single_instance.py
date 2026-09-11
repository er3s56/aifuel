"""Real Windows process handoff, without starting the UI or querying accounts."""
from pathlib import Path
import subprocess
import sys
import time
import unittest
import uuid


@unittest.skipUnless(sys.platform == "win32", "Windows named mutexes")
class SingleInstanceTests(unittest.TestCase):
    def spawn(self, code, name):
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", code, name],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(self.stop, process)
        return process

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    def owner(self, name):
        process = self.spawn("""
import sys
import single_instance as instance
assert instance.acquire(sys.argv[1])
print('ready', flush=True)
input()
instance.begin_shutdown()
print('closing', flush=True)
input()
""", name)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        return process

    def closing(self, owner):
        owner.stdin.write("\n")
        owner.stdin.flush()
        self.assertEqual(owner.stdout.readline().strip(), "closing")

    def contender(self, name, timeout=5):
        return self.spawn(f"""
import sys, time
import single_instance as instance
acquired = instance.acquire(sys.argv[1], timeout={timeout})
print(acquired, flush=True)
if acquired:
    time.sleep(0.5)
""", name)

    def test_repeated_exit_and_open_starts_exactly_one_replacement(self):
        for _ in range(3):
            name = "aifuel-test-" + uuid.uuid4().hex
            owner = self.owner(name)
            duplicate = self.contender(name)
            self.assertEqual(duplicate.communicate(timeout=3)[0].strip(), "False")
            self.closing(owner)
            waiting = [self.contender(name), self.contender(name)]
            time.sleep(.2)
            self.assertTrue(all(p.poll() is None for p in waiting), "Launch was dropped during shutdown")
            owner.communicate("\n", timeout=3)
            results = [p.communicate(timeout=5)[0].strip() for p in waiting]
            self.assertEqual(sorted(results), ["False", "True"])

    def test_timeout_does_not_start_a_second_instance(self):
        name = "aifuel-test-" + uuid.uuid4().hex
        owner = self.owner(name)
        self.closing(owner)
        pending = self.contender(name, timeout=.1)
        self.assertEqual(pending.communicate(timeout=3)[0].strip(), "False")
        self.assertIsNone(owner.poll())

    def test_abandoned_mutex_during_shutdown_can_be_recovered(self):
        name = "aifuel-test-" + uuid.uuid4().hex
        owner = self.owner(name)
        self.closing(owner)
        pending = self.contender(name)
        time.sleep(.2)
        self.assertIsNone(pending.poll())
        owner.kill()
        owner.communicate(timeout=3)
        self.assertEqual(pending.communicate(timeout=5)[0].strip(), "True")
