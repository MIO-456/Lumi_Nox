"""Repository checks against isolated files, never the full application's tests."""
from contextlib import redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("repo_docs_checker", ROOT / "scripts/check_docs.py")
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


class DocumentationVerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="repo verification ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "docs").mkdir()
        self.write("README.md", "# Fixture\n")
        self.write("AGENTS.md", "# Fixture\n")
        self.write("docs/README.md", "[Topic](topic.md)\n")
        self.write("docs/topic.md", "> **When to read**: example\n> **Review**: example\n")
        root_patch = patch.object(CHECKER, "ROOT", self.root)
        entry_patch = patch.object(CHECKER, "ENTRY_FILE", self.root / "AGENTS.md")
        root_patch.start()
        entry_patch.start()
        self.addCleanup(root_patch.stop)
        self.addCleanup(entry_patch.stop)

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def run_checker(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = CHECKER.main()
        return status, output.getvalue()

    def test_real_broken_link_still_fails(self):
        self.write("README.md", "[Actual link](docs/missing.md)\n")
        status, output = self.run_checker()
        self.assertNotEqual(status, 0)
        self.assertIn("docs/missing.md", output)
        self.assertIn("fail:", output)

    def test_unregistered_topic_still_fails(self):
        self.write("docs/unregistered.md", "# An unregistered topic\n")
        status, output = self.run_checker()
        self.assertNotEqual(status, 0)
        self.assertIn("docs/unregistered.md", output)
        self.assertIn("not listed", output)

    def test_percent_encoded_space_resolves_to_existing_file(self):
        self.write("docs/assets/with space.txt", "A real target\n")
        self.write("README.md", "[Download](docs/assets/with%20space.txt#example)\n")
        status, output = self.run_checker()
        self.assertEqual(status, 0, output)

    def test_inline_and_both_fence_styles_do_not_create_document_links(self):
        text = (
            "Use `[inline](missing-inline.md)` as syntax.\n"
            "Use ``[double](missing-double.md) and ` literal`` as syntax.\n"
            "```markdown\n[backtick](missing-backtick.md)\n```\n"
            "~~~markdown\n[tilde](missing-tilde.md)\n~~~\n"
            "[Actual topic](docs/topic.md)\n"
        )
        self.write("README.md", text)
        self.assertEqual(CHECKER.internal_targets(text), ["docs/topic.md"])
        status, output = self.run_checker()
        self.assertEqual(status, 0, output)

    def test_line_budgets_warn_without_failing_verification(self):
        self.write("AGENTS.md", "example\n" * (CHECKER.ENTRY_MAX_LINES + 1))
        self.write("docs/topic.md", "example\n" * (CHECKER.DOC_MAX_LINES + 1))
        problems, warnings = CHECKER.check_budgets()
        self.assertEqual(problems, [])
        self.assertTrue(any("AGENTS.md" in item for item in warnings))
        self.assertTrue(any("docs/topic.md" in item and "lines" in item for item in warnings))
        status, output = self.run_checker()
        self.assertEqual(status, 0, output)
        self.assertIn("warn:", output)
        self.assertNotIn("fail:", output)


def powershell():
    return shutil.which("powershell.exe") or shutil.which("pwsh") or shutil.which("pwsh.exe")


def bash():
    if os.name != "nt":
        return shutil.which("bash")
    # Windows' generic bash.exe can be a WSL launcher, not Git Bash.
    for directory in (os.environ.get("ProgramFiles", "C:/Program Files"),
                      os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")):
        path = Path(directory) / "Git/bin/bash.exe"
        if path.is_file():
            return str(path)
    return None


def ps_string(value):
    return "'" + str(value).replace("'", "''") + "'"


class InitVerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="repo init verification ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("init.ps1", "init.sh"):
            shutil.copyfile(ROOT / name, self.root / name)
        (self.root / "scripts").mkdir()
        (self.root / "tests").mkdir()
        # Only these tiny stdlib fixtures can be executed by the copied init.
        (self.root / "scripts/check_docs.py").write_text(
            "import sys\n"
            "assert sys.flags.utf8_mode == 1, 'UTF8 mode was not enabled'\n"
            "assert sys.stdout.encoding.lower().replace('-', '') == 'utf8'\n"
            "print('DOCS_UTF8: \\u6d4b\\u8bd5')\n", encoding="utf-8")
        (self.root / "tests/test_smoke.py").write_text(
            "import sys, unittest\n"
            "class Fixture(unittest.TestCase):\n"
            "    def test_utf8(self):\n"
            "        self.assertEqual(sys.flags.utf8_mode, 1)\n", encoding="utf-8")

    def run_ps(self, code):
        executable = powershell()
        if executable is None:
            self.skipTest("PowerShell is unavailable")
        wrapper = self.root / "run_fixture.ps1"
        wrapper.write_text(code, encoding="utf-8-sig")
        return subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(wrapper)], cwd=self.root.parent,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )

    def powershell_interpreter(self):
        if os.name != "nt":
            return self.shell_interpreter()
        interpreter = self.root / "python with spaces.cmd"
        interpreter.write_text(
            "@echo off\n"
            'if "%~1"=="--version" echo VERSION_VIA_SPACE_PATH\n'
            f'"{sys.executable.replace("%", "%%")}" %*\n'
            "exit /b %ERRORLEVEL%\n", encoding="utf-8", newline="\r\n")
        return interpreter

    def shell_interpreter(self):
        interpreter = self.root / "python with spaces.sh"
        interpreter.write_text(
            "#!/bin/sh\n"
            'if [ "${1:-}" = --version ]; then echo VERSION_VIA_SPACE_PATH; fi\n'
            f'exec {shlex.quote(Path(sys.executable).as_posix())} "$@"\n',
            encoding="utf-8", newline="\n")
        interpreter.chmod(0o755)
        return interpreter

    def test_powershell_missing_interpreter_cannot_report_success(self):
        result = self.run_ps(
            "$global:LASTEXITCODE = 0\n"
            f"& {ps_string(self.root / 'init.ps1')} -Python 'missing_python_for_repo_fixture'\n"
            "exit $LASTEXITCODE\n"
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("all checks passed", result.stdout)

    def test_powershell_non_executable_file_cannot_report_success(self):
        invalid = self.root / "not a real interpreter.exe"
        invalid.write_bytes(b"This fixture is not an executable.\n")
        result = self.run_ps(
            "$global:LASTEXITCODE = 0\n"
            f"& {ps_string(self.root / 'init.ps1')} -Python {ps_string(invalid)}\n"
            "exit $LASTEXITCODE\n"
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("all checks passed", result.stdout)

    def test_powershell_success_in_space_path_restores_parent_utf8_setting(self):
        interpreter = self.powershell_interpreter()
        result = self.run_ps(
            "$env:PYTHONUTF8 = '0'\n"
            "$env:PYTHONIOENCODING = 'ascii'\n"
            "$LASTEXITCODE = 91\n"  # A caller's local value must not hide the native result.
            f"& {ps_string(self.root / 'init.ps1')} -Python {ps_string(interpreter)}\n"
            "$checkSucceeded = $?\n"
            "$checkExitCode = $global:LASTEXITCODE\n"
            "if (-not $checkSucceeded -or $checkExitCode -ne 0) { exit 21 }\n"
            "if ($env:PYTHONUTF8 -ne '0' -or $env:PYTHONIOENCODING -ne 'ascii') { exit 22 }\n"
            "Write-Output 'PARENT_UTF8_PRESERVED'\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all checks passed", result.stdout)
        self.assertIn("DOCS_UTF8: \u6d4b\u8bd5", result.stdout)
        self.assertIn("PARENT_UTF8_PRESERVED", result.stdout)
        self.assertIn("VERSION_VIA_SPACE_PATH", result.stdout)

    def test_powershell_docs_failure_stops_before_tests(self):
        (self.root / "scripts/check_docs.py").write_text(
            "import sys\nsys.exit(7)\n", encoding="utf-8")
        (self.root / "tests/test_smoke.py").write_text(
            "from pathlib import Path\n"
            "(Path(__file__).resolve().parents[1] / 'tests_were_run').touch()\n",
            encoding="utf-8")
        result = self.run_ps(
            f"& {ps_string(self.root / 'init.ps1')} "
            f"-Python {ps_string(self.powershell_interpreter())}\n"
            "exit $LASTEXITCODE\n"
        )
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertFalse((self.root / "tests_were_run").exists())
        self.assertNotIn("== tests ==", result.stdout)
        self.assertNotIn("all checks passed", result.stdout)

    def test_bash_success_in_space_path_preserves_parent_utf8_setting(self):
        executable = bash()
        if executable is None:
            self.skipTest("POSIX Bash or explicit Git Bash is unavailable")
        interpreter = self.shell_interpreter()
        wrapper = self.root / "run_fixture.sh"
        wrapper.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            "export PYTHONUTF8=0\nexport PYTHONIOENCODING=ascii\n"
            f"export PYTHON={shlex.quote(interpreter.as_posix())}\n"
            f"source {shlex.quote((self.root / 'init.sh').as_posix())}\n"
            "test \"$PYTHONUTF8\" = 0\n"
            "test \"$PYTHONIOENCODING\" = ascii\n"
            "printf 'PARENT_UTF8_PRESERVED\\n'\n", encoding="utf-8", newline="\n")
        result = subprocess.run(
            [executable, wrapper.as_posix()], cwd=self.root.parent,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all checks passed", result.stdout)
        self.assertIn("DOCS_UTF8: \u6d4b\u8bd5", result.stdout)
        self.assertIn("PARENT_UTF8_PRESERVED", result.stdout)
        self.assertIn("VERSION_VIA_SPACE_PATH", result.stdout)


if __name__ == "__main__":
    unittest.main()
