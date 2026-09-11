"""Compile and run the native selector regression without touching Explorer."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPILER = ROOT / "build/windhawk-runtime/Compiler/bin/clang++.exe"


@unittest.skipUnless(os.name == "nt" and COMPILER.is_file(), "Windhawk compiler not available")
class NativeTaskbarTests(unittest.TestCase):
    def test_popup_cannot_take_over_the_quota_lease(self):
        with tempfile.TemporaryDirectory(prefix=".test-state-native-", dir=ROOT) as directory:
            executable = Path(directory) / "lease-test.exe"
            built = subprocess.run([
                str(COMPILER), "-std=c++23", "-static", "-DUNICODE", "-D_UNICODE",
                "-target", "x86_64-w64-mingw32",
                str(ROOT / "native/tests/taskbar_lease_test.cpp"), "-o", str(executable),
            ], capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            tested = subprocess.run([str(executable)], capture_output=True, text=True,
                                    timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)


if __name__ == "__main__":
    unittest.main()
