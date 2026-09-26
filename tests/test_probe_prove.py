"""
tests/test_probe_prove.py — Tests for the probe and prove commands,
and for the two antibody_plugin.py fixes.

Reuses helpers from test_runner.py.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

# Re-use helpers from test_runner.py by loading the module from its file path,
# so this works regardless of whether tests/ has an __init__.py.
_RUNNER_MOD_PATH = Path(__file__).parent / "test_runner.py"
_spec = importlib.util.spec_from_file_location("_test_runner_helpers", _RUNNER_MOD_PATH)
_tr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tr)

_commit_all = _tr._commit_all
_git = _tr._git
_init_git_repo = _tr._init_git_repo
_make_venv = _tr._make_venv
_write = _tr._write
setup_antibody_env = _tr.setup_antibody_env
write_candidates_json = _tr.write_candidates_json
write_setup_json = _tr.write_setup_json

SCRIPTS_DIR = (
    Path(__file__).parent.parent / ".bob" / "skills" / "recurrence-audit" / "scripts"
)
RUNNER = SCRIPTS_DIR / "antibody.py"
PLUGIN = SCRIPTS_DIR / "antibody_plugin.py"


# ---------------------------------------------------------------------------
# Shared project factory
# ---------------------------------------------------------------------------

def _make_project(tmp_path: Path, pkg_src_buggy: str, pkg_src_fixed: str,
                  test_src: str | None = None,
                  extra_commits: list[tuple[str, dict[str, str]]] | None = None,
                  pkg_name: str = "mypkg") -> tuple[Path, str, str]:
    """
    Build a minimal git project with a bug commit followed by a fix commit.

    Returns (repo_path, bug_sha, fix_sha).
    extra_commits: list of (message, {relative_path: content}) applied after the fix.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    # Initial commit: buggy code
    _write(repo / pkg_name / "__init__.py", "")
    _write(repo / pkg_name / "core.py", pkg_src_buggy)
    if test_src:
        _write(repo / "tests" / "test_core.py", test_src)
    _write(
        repo / "setup.py",
        f"from setuptools import setup, find_packages; "
        f"setup(name={pkg_name!r}, packages=find_packages())",
    )
    _commit_all(repo, "initial commit")

    # Fix commit
    _write(repo / pkg_name / "core.py", pkg_src_fixed)
    fix_sha = _commit_all(repo, "fix: correct behaviour")

    if extra_commits:
        for msg, files in extra_commits:
            for rel, content in files.items():
                _write(repo / rel, content)
            _commit_all(repo, msg)

    return repo, "initial", fix_sha


def _write_patch_from_git(repo: Path, fix_sha: str, pkg_name: str, ab: Path) -> Path:
    patch_result = subprocess.run(
        ["git", "format-patch", "--stdout", "-1", fix_sha, "--", f"{pkg_name}/core.py"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    patch_path = ab / "diffs" / f"{fix_sha}.patch"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(patch_result.stdout)
    return patch_path


def _run_runner_cmd(cmd: list[str], tmp_path: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["_ANTIBODY_ROOT"] = str(tmp_path)
    return subprocess.run(
        ["python3.12", str(RUNNER), *cmd],
        capture_output=True,
        text=True,
        env=env,
    )


def _read_proof(tmp_path: Path, name: str, sha: str) -> dict:
    p = tmp_path / ".antibody" / name / "proofs" / f"{sha}.json"
    assert p.exists(), f"proof not found: {p}"
    return json.loads(p.read_text())


def _read_probe(tmp_path: Path, name: str, sha: str) -> dict:
    p = tmp_path / ".antibody" / name / "probes" / f"{sha}.json"
    assert p.exists(), f"probe not found: {p}"
    return json.loads(p.read_text())


# ---------------------------------------------------------------------------
# Helper: write candidates.json pointing to the fix sha and src_files
# ---------------------------------------------------------------------------

def _setup_target(tmp_path: Path, name: str, repo: Path, fix_sha: str,
                  pkg_name: str = "mypkg",
                  extra_venv_packages: list[str] | None = None) -> Path:
    ab, _ = setup_antibody_env(tmp_path, name, repo,
                               extra_venv_packages=extra_venv_packages)
    _write_patch_from_git(repo, fix_sha, pkg_name, ab)
    write_setup_json(ab, name, tmp_path / "targets", collected=1)
    write_candidates_json(ab, [{
        "sha": fix_sha,
        "subject": "fix: correct behaviour",
        "src_files": [f"{pkg_name}/core.py"],
        "diff_lines": 2,
        "status": "candidate",
        "reason": "",
    }])
    return ab


# ===========================================================================
# prove tests
# ===========================================================================

class TestProve:

    def test_proven(self, tmp_path):
        """A good test — passes with fix, fails on AssertionError without fix → PROVEN."""
        buggy = "def add(a, b):\n    return a - b  # BUG\n"
        fixed = "def add(a, b):\n    return a + b  # FIXED\n"
        test_src = textwrap.dedent("""\
            from mypkg.core import add
            def test_add():
                assert add(2, 3) == 5
        """)

        repo, _, fix_sha = _make_project(tmp_path, buggy, fixed)
        _write(repo / "tests" / "test_core.py", test_src)
        _commit_all(repo, "add test")
        # Re-get fix_sha (it's the second commit, before the test commit)
        fix_sha_result = subprocess.run(
            ["git", "log", "--format=%H", "--no-merges"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        )
        shas = fix_sha_result.stdout.strip().splitlines()
        # shas[0] = add test, shas[1] = fix, shas[2] = initial
        fix_sha = shas[1]

        name = "prove-good"
        ab = _setup_target(tmp_path, name, repo, fix_sha)

        result = _run_runner_cmd(
            ["prove", name, fix_sha, "tests/test_core.py"],
            tmp_path,
        )
        assert "PROVEN" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
        proof = _read_proof(tmp_path, name, fix_sha)
        assert proof["verdict"] == "PROVEN"

    def test_not_proven_missing_function(self, tmp_path):
        """
        Test fails without fix because a function added by the fix is missing
        → NOT PROVEN at step 2 (AttributeError, not AssertionError).
        """
        # Bug: function doesn't exist at all
        buggy = "# no validate function\n"
        fixed = "def validate(x):\n    if x < 0:\n        raise ValueError('negative')\n"
        test_src = textwrap.dedent("""\
            from mypkg.core import validate
            def test_validate():
                validate(1)
                try:
                    validate(-1)
                    assert False, 'should raise'
                except ValueError:
                    pass
        """)

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        _write(repo / "mypkg" / "__init__.py", "")
        _write(repo / "mypkg" / "core.py", buggy)
        _write(repo / "tests" / "test_core.py", test_src)
        _write(repo / "setup.py",
               "from setuptools import setup, find_packages; setup(name='mypkg', packages=find_packages())")
        _commit_all(repo, "initial commit")
        _write(repo / "mypkg" / "core.py", fixed)
        fix_sha = _commit_all(repo, "fix: add validate function")

        name = "prove-missing-fn"
        ab = _setup_target(tmp_path, name, repo, fix_sha)

        result = _run_runner_cmd(
            ["prove", name, fix_sha, "tests/test_core.py"],
            tmp_path,
        )
        assert "NOT PROVEN" in result.stdout, (
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        proof = _read_proof(tmp_path, name, fix_sha)
        assert proof["verdict"] == "NOT PROVEN"
        assert "step 2" in proof["step"]

    def test_not_proven_still_passes(self, tmp_path):
        """Test passes both with and without the fix → NOT PROVEN at step 2."""
        buggy = "def compute(x):\n    return x * 2\n"
        fixed = "def compute(x):\n    return x * 2  # same behaviour\n"
        test_src = textwrap.dedent("""\
            from mypkg.core import compute
            def test_compute():
                assert compute(3) == 6
        """)

        repo, _, fix_sha = _make_project(tmp_path, buggy, fixed)
        _write(repo / "tests" / "test_core.py", test_src)
        _commit_all(repo, "add test")

        # Get the fix sha (second from the head)
        fix_sha_result = subprocess.run(
            ["git", "log", "--format=%H", "--no-merges"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        )
        shas = fix_sha_result.stdout.strip().splitlines()
        fix_sha = shas[1]

        name = "prove-still-passes"
        ab = _setup_target(tmp_path, name, repo, fix_sha)

        result = _run_runner_cmd(
            ["prove", name, fix_sha, "tests/test_core.py"],
            tmp_path,
        )
        assert "NOT PROVEN" in result.stdout, (
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        proof = _read_proof(tmp_path, name, fix_sha)
        assert proof["verdict"] == "NOT PROVEN"
        assert "step 2" in proof["step"]

    def test_not_proven_reads_source(self, tmp_path):
        """Test that uses inspect.getsource is rejected."""
        buggy = "def f():\n    return 0\n"
        fixed = "def f():\n    return 1\n"
        test_src = textwrap.dedent("""\
            import inspect
            from mypkg.core import f
            def test_f():
                src = inspect.getsource(f)
                assert 'return 1' in src
        """)

        repo, _, fix_sha = _make_project(tmp_path, buggy, fixed)
        _write(repo / "tests" / "test_source.py", test_src)
        _commit_all(repo, "add test")

        fix_sha_result = subprocess.run(
            ["git", "log", "--format=%H", "--no-merges"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        )
        shas = fix_sha_result.stdout.strip().splitlines()
        fix_sha = shas[1]

        name = "prove-reads-source"
        ab = _setup_target(tmp_path, name, repo, fix_sha)

        result = _run_runner_cmd(
            ["prove", name, fix_sha, "tests/test_source.py"],
            tmp_path,
        )
        assert "NOT PROVEN" in result.stdout or "rejected" in result.stdout.lower(), (
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        proof = _read_proof(tmp_path, name, fix_sha)
        assert proof["verdict"] == "NOT PROVEN"
        assert "source" in proof["step"].lower()

    def test_not_proven_flaky(self, tmp_path):
        """
        A test that fails only 1 time in 3 with the bug back → NOT PROVEN at step 2.

        We simulate this by making the test read a counter from a temp file
        that we pre-seed to fail once then pass.
        Actually simpler: make the check write a counter file and only fail the first time.
        But that's complex. Simpler approach: the test fails with bug back due to
        random.random() > threshold — but that's non-deterministic.

        Better: use a file-based counter. The test is NOT PROVEN because we
        control the counter to fail only once. Since prove runs 3 times with bug back
        and all 3 must fail, 1/3 means NOT PROVEN.

        Implementation: the test reads a counter file and fails only when counter==1.
        We pre-seed the counter at 1. Then after first run it becomes 2 (passes).
        """
        buggy = "def f():\n    return 0\n"
        fixed = "def f():\n    return 1\n"

        # The test uses a side-effect counter
        test_src = textwrap.dedent("""\
            import os, tempfile
            from mypkg.core import f

            _COUNTER_FILE = os.path.join(tempfile.gettempdir(), 'antibody_flaky_counter.txt')

            def test_flaky():
                # Read counter
                try:
                    count = int(open(_COUNTER_FILE).read().strip())
                except Exception:
                    count = 0
                # Increment
                with open(_COUNTER_FILE, 'w') as fp:
                    fp.write(str(count + 1))
                # Only fail on the FIRST call (count == 0)
                if count == 0:
                    assert f() == 1  # fails with bug (returns 0)
                else:
                    pass  # passes after first call
        """)

        # Reset the counter file
        import tempfile as _tmpfile
        counter_file = Path(_tmpfile.gettempdir()) / "antibody_flaky_counter.txt"
        counter_file.write_text("0")

        repo, _, fix_sha = _make_project(tmp_path, buggy, fixed)
        _write(repo / "tests" / "test_flaky.py", test_src)
        _commit_all(repo, "add flaky test")

        fix_sha_result = subprocess.run(
            ["git", "log", "--format=%H", "--no-merges"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        )
        shas = fix_sha_result.stdout.strip().splitlines()
        fix_sha = shas[1]

        name = "prove-flaky"
        ab = _setup_target(tmp_path, name, repo, fix_sha)

        result = _run_runner_cmd(
            ["prove", name, fix_sha, "tests/test_flaky.py"],
            tmp_path,
        )
        assert "NOT PROVEN" in result.stdout, (
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        proof = _read_proof(tmp_path, name, fix_sha)
        assert proof["verdict"] == "NOT PROVEN"

        # Clean up
        counter_file.unlink(missing_ok=True)

    def test_prove_concurrent(self, tmp_path):
        """
        Two prove calls started concurrently on two different fixes give
        the same answer as when called alone.
        """
        # Fix 1: add → returns correct sum
        buggy1 = "def add(a, b):\n    return a - b\n"
        fixed1 = "def add(a, b):\n    return a + b\n"
        test1 = textwrap.dedent("""\
            from mypkg1.core import add
            def test_add():
                assert add(2, 3) == 5
        """)

        # Fix 2: mul → returns correct product
        buggy2 = "def mul(a, b):\n    return a + b\n"
        fixed2 = "def mul(a, b):\n    return a * b\n"
        test2 = textwrap.dedent("""\
            from mypkg2.core import mul
            def test_mul():
                assert mul(2, 3) == 6
        """)

        def make_project(pkg_name, buggy, fixed, test_src, tmp_sub):
            repo = tmp_sub / "repo"
            repo.mkdir(parents=True)
            _init_git_repo(repo)
            _write(repo / pkg_name / "__init__.py", "")
            _write(repo / pkg_name / "core.py", buggy)
            _write(repo / "tests" / "test_core.py", test_src)
            _write(repo / "setup.py",
                   f"from setuptools import setup, find_packages; "
                   f"setup(name={pkg_name!r}, packages=find_packages())")
            _commit_all(repo, "initial commit")
            _write(repo / pkg_name / "core.py", fixed)
            sha = _commit_all(repo, "fix: correct behaviour")
            return repo, sha

        tmp1 = tmp_path / "p1"
        tmp2 = tmp_path / "p2"
        tmp1.mkdir()
        tmp2.mkdir()

        repo1, sha1 = make_project("mypkg1", buggy1, fixed1, test1, tmp1)
        repo2, sha2 = make_project("mypkg2", buggy2, fixed2, test2, tmp2)

        # Use a shared tmp_path for both (each has its own name)
        ab1 = _setup_target(tmp_path, "concurrent1", repo1, sha1, pkg_name="mypkg1")
        ab2 = _setup_target(tmp_path, "concurrent2", repo2, sha2, pkg_name="mypkg2")

        results = {}

        def run_prove(name, sha, test_path, key):
            r = _run_runner_cmd(["prove", name, sha, test_path], tmp_path)
            results[key] = r

        t1 = threading.Thread(target=run_prove,
                              args=("concurrent1", sha1, "tests/test_core.py", "r1"))
        t2 = threading.Thread(target=run_prove,
                              args=("concurrent2", sha2, "tests/test_core.py", "r2"))
        t1.start()
        t2.start()
        t1.join(timeout=120)
        t2.join(timeout=120)

        r1 = results.get("r1")
        r2 = results.get("r2")
        assert r1 is not None, "Thread 1 did not finish"
        assert r2 is not None, "Thread 2 did not finish"

        assert "PROVEN" in r1.stdout, f"r1 stdout: {r1.stdout}\nstderr: {r1.stderr}"
        assert "PROVEN" in r2.stdout, f"r2 stdout: {r2.stdout}\nstderr: {r2.stderr}"


# ===========================================================================
# probe tests
# ===========================================================================

class TestProbe:

    def _make_probe_project(self, tmp_path, buggy, fixed, pkg_name="mypkg"):
        """Set up a project and return (ab, fix_sha, repo)."""
        repo, _, fix_sha = _make_project(tmp_path, buggy, fixed, pkg_name=pkg_name)
        name = "probe-test"
        ab = _setup_target(tmp_path, name, repo, fix_sha, pkg_name=pkg_name)
        return ab, fix_sha, repo, name

    def test_probe_constant_invalid_prints_nothing(self, tmp_path):
        """A check that prints a constant (same on both sides) → INVALID (prints nothing)
        if the check is empty, or INVALID (never reaches changed code) if it doesn't
        call the changed code."""
        buggy = "def f():\n    return 0\n"
        fixed = "def f():\n    return 1\n"
        ab, fix_sha, repo, name = self._make_probe_project(tmp_path, buggy, fixed)

        # A check that just prints a constant — no call to the package at all
        check = tmp_path / "check_constant.py"
        check.write_text("print('hello')\n")

        result = _run_runner_cmd(
            ["probe", name, fix_sha, str(check)],
            tmp_path,
        )
        out = result.stdout + result.stderr
        # Must be INVALID — either "never reaches the changed code" or equivalent
        assert "INVALID" in out, f"Expected INVALID, got: {out}"
        # Must NOT ever say NO CHANGE FOUND
        assert "NO CHANGE FOUND" not in out, f"Got NO CHANGE FOUND: {out}"

    def test_probe_never_reaches_changed_code(self, tmp_path):
        """Check calls the package but never executes the changed line → INVALID (never reaches…)."""
        buggy = "def f():\n    return 0\ndef g():\n    return 99\n"
        fixed = "def f():\n    return 1\ndef g():\n    return 99\n"
        ab, fix_sha, repo, name = self._make_probe_project(tmp_path, buggy, fixed)

        # A check that calls g() only — never f(), which is the changed function
        check = tmp_path / "check_unreachable.py"
        check.write_text(textwrap.dedent("""\
            from mypkg.core import g
            result = g()
            print(result)
        """))

        result = _run_runner_cmd(
            ["probe", name, fix_sha, str(check)],
            tmp_path,
        )
        out = result.stdout + result.stderr
        assert "INVALID" in out, f"Expected INVALID (never reaches…), got: {out}"
        assert "NO CHANGE FOUND" not in out

    def test_probe_changed_output(self, tmp_path):
        """A check that captures the bug symptom → CHANGED."""
        buggy = "def f():\n    return 0\n"
        fixed = "def f():\n    return 1\n"
        ab, fix_sha, repo, name = self._make_probe_project(tmp_path, buggy, fixed)

        check = tmp_path / "check_changed.py"
        check.write_text(textwrap.dedent("""\
            from mypkg.core import f
            print(f())
        """))

        result = _run_runner_cmd(
            ["probe", name, fix_sha, str(check)],
            tmp_path,
        )
        out = result.stdout + result.stderr
        assert "CHANGED" in out, f"Expected CHANGED, got: {out}"

    def test_probe_data_only_inconclusive(self, tmp_path):
        """
        Fix edits only a module-level constant (data-only change).
        Check's output doesn't change → INCONCLUSIVE (data-only change), never NO CHANGE FOUND.
        """
        # The fix changes a module-level dict (pure data change)
        buggy = textwrap.dedent("""\
            # Module-level table
            MAPPING = {'a': 1, 'b': 2}

            def lookup(key):
                return MAPPING.get(key, -1)
        """)
        fixed = textwrap.dedent("""\
            # Module-level table — 'c' added by the fix
            MAPPING = {'a': 1, 'b': 2, 'c': 3}

            def lookup(key):
                return MAPPING.get(key, -1)
        """)

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        _write(repo / "mypkg" / "__init__.py", "")
        _write(repo / "mypkg" / "core.py", buggy)
        _write(repo / "setup.py",
               "from setuptools import setup, find_packages; setup(name='mypkg', packages=find_packages())")
        _commit_all(repo, "initial commit")
        _write(repo / "mypkg" / "core.py", fixed)
        fix_sha = _commit_all(repo, "fix: add 'c' to MAPPING")

        name = "probe-data-only"
        ab, _ = setup_antibody_env(tmp_path, name, repo)
        _write_patch_from_git(repo, fix_sha, "mypkg", ab)
        write_setup_json(ab, name, tmp_path / "targets", collected=1)
        write_candidates_json(ab, [{
            "sha": fix_sha,
            "subject": "fix: add 'c' to MAPPING",
            "src_files": ["mypkg/core.py"],
            "diff_lines": 2,
            "status": "candidate",
            "reason": "",
        }])

        # Check: only calls lookup('a') — output is 1 on both sides
        check = tmp_path / "check_data_only.py"
        check.write_text(textwrap.dedent("""\
            from mypkg.core import lookup
            print(lookup('a'))
        """))

        result = _run_runner_cmd(
            ["probe", name, fix_sha, str(check)],
            tmp_path,
        )
        out = result.stdout + result.stderr
        assert "INCONCLUSIVE" in out, f"Expected INCONCLUSIVE (data-only change), got: {out}"
        assert "NO CHANGE FOUND" not in out


# ===========================================================================
# antibody_plugin.py fixes
# ===========================================================================

class TestPluginFixes:
    """Tests for the two antibody_plugin.py fixes."""

    def _run_pytest_with_plugin(self, test_content: str, tmp_path: Path) -> dict:
        """
        Write a test file, run pytest with antibody_plugin, return the report dict.
        Uses the system Python (or python3.12) since the plugin only needs stdlib + pytest.
        """
        test_file = tmp_path / "test_subject.py"
        test_file.write_text(test_content)

        report_file = tmp_path / "report.json"

        plugin_dir = str(PLUGIN.parent)
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["ANTIBODY_REPORT_PATH"] = str(report_file)
        env["PYTHONPATH"] = plugin_dir + (":" + env.get("PYTHONPATH", ""))

        subprocess.run(
            [
                "python3.12", "-m", "pytest",
                "-p", "no:cacheprovider",
                "-p", "antibody_plugin",
                str(test_file),
                "-v",
                "--tb=short",
            ],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
        )

        assert report_file.exists(), "Plugin did not write report"
        return json.loads(report_file.read_text())

    def test_setup_error_counts_as_failure(self, tmp_path):
        """
        Fix 1: A test whose fixture raises during setup must appear in 'failed'.
        """
        test_src = textwrap.dedent("""\
            import pytest

            @pytest.fixture
            def bad_fixture():
                raise RuntimeError("setup explodes")

            def test_uses_bad_fixture(bad_fixture):
                pass  # never reached
        """)
        report = self._run_pytest_with_plugin(test_src, tmp_path)
        assert "test_uses_bad_fixture" in " ".join(report["failed"]), (
            f"Setup error not counted as failure. failed={report['failed']}"
        )

    def test_teardown_error_counts_as_failure(self, tmp_path):
        """
        Fix 1: A test whose fixture raises during teardown must appear in 'failed'.
        """
        test_src = textwrap.dedent("""\
            import pytest

            @pytest.fixture
            def bad_teardown():
                yield
                raise RuntimeError("teardown explodes")

            def test_uses_bad_teardown(bad_teardown):
                pass  # passes, but teardown errors
        """)
        report = self._run_pytest_with_plugin(test_src, tmp_path)
        assert "test_uses_bad_teardown" in " ".join(report["failed"]), (
            f"Teardown error not counted as failure. failed={report['failed']}"
        )

    def test_xfail_recorded_separately(self, tmp_path):
        """
        Fix 2: xfailed tests must NOT be in 'failed' but must be in 'xfailed' count.
        """
        test_src = textwrap.dedent("""\
            import pytest

            @pytest.mark.xfail
            def test_expected_failure():
                assert 1 == 2  # fails as expected
        """)
        report = self._run_pytest_with_plugin(test_src, tmp_path)
        assert report["failed"] == [], (
            f"xfailed test should not be in failed. failed={report['failed']}"
        )
        assert report["xfailed"] >= 1, (
            f"xfailed count should be >= 1, got {report['xfailed']}"
        )

    def test_xpass_recorded_separately(self, tmp_path):
        """
        Fix 2: xpassed tests must NOT be in 'failed' but must be in 'xpassed' count.
        """
        test_src = textwrap.dedent("""\
            import pytest

            @pytest.mark.xfail
            def test_unexpected_pass():
                assert 1 == 1  # passes unexpectedly
        """)
        report = self._run_pytest_with_plugin(test_src, tmp_path)
        # xpass without strict=True: test is not a failure
        assert report.get("xpassed", 0) >= 1, (
            f"xpassed count should be >= 1, got {report.get('xpassed', 0)}"
        )

    def test_skipped_recorded_separately(self, tmp_path):
        """
        Fix 2: skipped tests must NOT be in 'failed' but must be in 'skipped' count.
        """
        test_src = textwrap.dedent("""\
            import pytest

            @pytest.mark.skip(reason="not implemented")
            def test_skipped():
                pass
        """)
        report = self._run_pytest_with_plugin(test_src, tmp_path)
        assert report["failed"] == [], (
            f"skipped test should not be in failed. failed={report['failed']}"
        )
        assert report["skipped"] >= 1, (
            f"skipped count should be >= 1, got {report['skipped']}"
        )


# ===========================================================================
# Slowdown test example (ratio-based, not raw threshold)
# ===========================================================================

class TestSlowdownExample:
    """
    Demonstrates that performance tests must compare growth ratios,
    not raw times against thresholds.
    """

    def test_linear_ratio_under_8(self, tmp_path):
        """
        A linear function's time for 4n vs n should be < 8x (roughly 4x for linear).
        This tests the principle without needing the actual package.
        """
        import time as time_mod

        def linear_work(n: int) -> None:
            _ = list(range(n))

        n = 1000
        t0 = time_mod.perf_counter()
        for _ in range(10):
            linear_work(n)
        t1 = time_mod.perf_counter()
        time_n = t1 - t0

        t0 = time_mod.perf_counter()
        for _ in range(10):
            linear_work(4 * n)
        t1 = time_mod.perf_counter()
        time_4n = t1 - t0

        ratio = time_4n / max(time_n, 1e-9)
        assert ratio < 8, (
            f"Linear work should have ratio < 8, got {ratio:.2f}"
        )
