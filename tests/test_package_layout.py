"""The organized tree must keep the no-install entry points and resource root."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from core.paths import PROJECT_ROOT


ROOT = Path(__file__).resolve().parents[1]


class PackageLayoutTests(unittest.TestCase):
    def test_demo_runs_from_an_unrelated_working_directory_without_dependencies(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "-S", str(ROOT / "main.py")],
                cwd=folder, capture_output=True, text=True, timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("demo complete", result.stdout)

    def test_package_imports_remain_lightweight(self):
        code = (
            "import sys; import core, dialogue, audio, settings; "
            "from core.event_bus import EventBus; "
            "from core.speech_output_arbiter import SpeechOutputArbiter; "
            "from settings.voice_config import get_speaker_config; "
            "assert not {'numpy', 'dashscope', 'pyaudiowpatch', 'openai'} & set(sys.modules)"
        )
        result = subprocess.run([sys.executable, "-S", "-c", code], cwd=ROOT,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_resource_root_and_single_entry_file(self):
        self.assertEqual(PROJECT_ROOT, ROOT)
        self.assertEqual(sorted(path.name for path in ROOT.glob("*.py")), ["main.py"])


if __name__ == "__main__":
    unittest.main()
