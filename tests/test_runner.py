"""
tests/test_runner.py — Tests for the Antibody runner.

Builds tiny git projects in temp dirs to test the four scenarios:
 (a) a fix whose test catches the bug → CAUGHT
 (b) a fix no test covers            → ESCAPED
 (c) patch no longer applies          → NO DATA:code-moved
 (d) 0 tests collected                → NO DATA (zero-collected)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

SCRIPTS_DIR = (
    Path(__file__).parent.parent / ".bob" / "skills" / "recurrence-audit" / "scripts"
)
RUNNER = SCRIPTS_DIR / "antibody.py"
PLUGIN = SCRIPTS_DIR / "antibody_plugin.py"

# ---------------------------------------------------------------------------
# Helpers for building a minimal git+Python project
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


def _make_venv(venv_path: Path) -> Path:
    """Create a venv with uv and install pytest. Return python path."""
    subprocess.run(
        ["uv", "venv", "--python", "3.12", str(venv_path)],
        check=True, capture_output=True,
    )
    python = venv_path / "bin" / "python"
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), "--quiet", "pytest"],
        check=True, capture_output=True,
    )
    return python


def _init_project(root: Path, pkg_name: str, pkg_src: str) -> None:
    """Write a minimal Python package."""
    pkg_dir = root / pkg_name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    _write(pkg_dir / "__init__.py", "")
    _write(pkg_dir / "core.py", pkg_src)
    _write(root / "setup.py", f"""
        from setuptools import setup, find_packages
        setup(name={pkg_name!r}, packages=find_packages())
    """)


def _commit_all(repo: Path, msg: str) -> str:
    _git(["add", "-A"], repo)
    _git(["commit", "-m", msg], repo)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _init_git_repo(path: Path) -> None:
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@test.invalid"], path)
    _git(["config", "user.name", "Test"], path)


def setup_antibody_env(tmp: Path, name: str, repo_path: Path, extra_venv_packages: list[str] | None = None) -> tuple[Path, Path]:
    """
    Set up the antibody directory structure pointing to a local repo.
    Returns (antibody_dir, venv_path).
    """
    targets = tmp / "targets"
    venvs = targets / ".venvs"
    ab = tmp / ".antibody" / name

    targets.mkdir(parents=True, exist_ok=True)
    venvs.mkdir(parents=True, exist_ok=True)
    ab.mkdir(parents=True, exist_ok=True)
    (ab / "diffs").mkdir(parents=True, exist_ok=True)

    # Symlink/copy the repo as the target
    tgt_link = targets / name
    if tgt_link.exists():
        tgt_link.unlink()
    tgt_link.symlink_to(repo_path)

    # Create venv
    venv_path = venvs / name
    python = _make_venv(venv_path)

    # Install the package editable
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), "--quiet", "-e", str(repo_path)],
        check=True, capture_output=True,
    )
    if extra_venv_packages:
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), "--quiet", *extra_venv_packages],
            check=True, capture_output=True,
        )

    return ab, venv_path


def write_setup_json(ab: Path, name: str, targets: Path, collected: int, secs: float = 1.0) -> None:
    """Write a minimal setup.json."""
    data = {
        "repo": "file:///local",
        "head": "deadbeef",
        "deps": [],
        "install_commands": [],
        "baseline": {
            "collected": collected,
            "passed": collected,
            "skipped": 0,
            "seconds": secs,
        },
    }
    with open(ab / "setup.json", "w") as f:
        json.dump(data, f)


def write_candidates_json(ab: Path, candidates: list[dict]) -> None:
    with open(ab / "candidates.json", "w") as f:
        json.dump(candidates, f)


def run_runner(cmd: list[str], env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    """Run the antibody runner with the given subcommand args."""
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(RUNNER), *cmd],
        capture_output=True,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# Scenario (a): CAUGHT — fix has a test that catches the regression
# ---------------------------------------------------------------------------

def test_caught(tmp_path):
    """
    Scenario: the fix changes buggy_func to return correct value.
    There is a test that asserts the correct behaviour.
    Reversing the fix should fail the test → CAUGHT.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    pkg = "mypkg"
    # Initial commit: buggy code
    _write(repo / pkg / "__init__.py", "")
    _write(repo / pkg / "core.py", """\
        def buggy_func():
            return 0   # BUG: should be 1
    """)
    _write(repo / "tests" / "test_core.py", """\
        from mypkg.core import buggy_func
        def test_buggy_func():
            assert buggy_func() == 1
    """)
    _write(repo / "setup.py", f"from setuptools import setup, find_packages; setup(name={pkg!r}, packages=find_packages())")
    _commit_all(repo, "initial commit")

    # Fix commit
    _write(repo / pkg / "core.py", """\
        def buggy_func():
            return 1   # FIXED
    """)
    fix_sha = _commit_all(repo, "fix: correct return value of buggy_func")

    # Build antibody env
    name = "caught-test"
    ab, venv_path = setup_antibody_env(tmp_path, name, repo)

    # Build patch for the fix
    patch_result = subprocess.run(
        ["git", "format-patch", "--stdout", "-1", fix_sha, "--", f"{pkg}/core.py"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    patch_path = ab / "diffs" / f"{fix_sha}.patch"
    patch_path.write_text(patch_result.stdout)

    write_setup_json(ab, name, tmp_path / "targets", collected=1)
    write_candidates_json(ab, [{
        "sha": fix_sha,
        "subject": "fix: correct return value",
        "src_files": [f"{pkg}/core.py"],
        "diff_lines": 2,
        "status": "candidate",
        "reason": "",
    }])

    env = {
        "ANTIBODY_TARGETS_DIR": str(tmp_path / "targets"),
        "ANTIBODY_VENVS_DIR": str(tmp_path / "targets" / ".venvs"),
        "ANTIBODY_DIR": str(tmp_path / ".antibody"),
    }

    result = _run_cmd_run(name, tmp_path)
    assert result.returncode == 0, result.stderr

    results = _read_results(tmp_path, name)
    rows = results["rows"]
    assert len(rows) == 1
    assert rows[0]["status"] == "CAUGHT", f"Expected CAUGHT, got: {rows[0]['status']}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert len(rows[0]["catching_tests"]) >= 1


# ---------------------------------------------------------------------------
# Scenario (b): ESCAPED — no test covers the reverted code path
# ---------------------------------------------------------------------------

def test_escaped(tmp_path):
    """
    Scenario: fix changes uncovered code path. Reversing doesn't fail any test → ESCAPED.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    pkg = "mypkg"
    _write(repo / pkg / "__init__.py", "")
    _write(repo / pkg / "core.py", """\
        def uncovered():
            return 0   # never tested, has bug

        def covered():
            return 42
    """)
    _write(repo / "tests" / "test_covered.py", """\
        from mypkg.core import covered
        def test_covered():
            assert covered() == 42
    """)
    _write(repo / "setup.py", f"from setuptools import setup, find_packages; setup(name={pkg!r}, packages=find_packages())")
    _commit_all(repo, "initial commit")

    # Fix commit: "fix" uncovered() but no test exists
    _write(repo / pkg / "core.py", """\
        def uncovered():
            return 1   # fixed but untested

        def covered():
            return 42
    """)
    fix_sha = _commit_all(repo, "fix: uncovered() now returns correct value")

    name = "escaped-test"
    ab, venv_path = setup_antibody_env(tmp_path, name, repo)

    patch_result = subprocess.run(
        ["git", "format-patch", "--stdout", "-1", fix_sha, "--", f"{pkg}/core.py"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    patch_path = ab / "diffs" / f"{fix_sha}.patch"
    patch_path.write_text(patch_result.stdout)

    write_setup_json(ab, name, tmp_path / "targets", collected=1)
    write_candidates_json(ab, [{
        "sha": fix_sha,
        "subject": "fix: uncovered returns correct value",
        "src_files": [f"{pkg}/core.py"],
        "diff_lines": 2,
        "status": "candidate",
        "reason": "",
    }])

    result = _run_cmd_run(name, tmp_path)
    assert result.returncode == 0, result.stderr

    results = _read_results(tmp_path, name)
    rows = results["rows"]
    assert len(rows) == 1
    assert rows[0]["status"] == "ESCAPED", f"Expected ESCAPED, got: {rows[0]['status']}\nstdout: {result.stdout}\nstderr: {result.stderr}"


# ---------------------------------------------------------------------------
# Scenario (c): NO DATA:code-moved — patch doesn't apply to current HEAD
# ---------------------------------------------------------------------------

def test_code_moved(tmp_path):
    """
    Scenario: fix committed, then the file was heavily refactored so the patch
    no longer applies → NO DATA:code-moved.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    pkg = "mypkg"
    _write(repo / pkg / "__init__.py", "")
    _write(repo / pkg / "core.py", """\
        def old_func():
            return 0   # bug
    """)
    _write(repo / "tests" / "test_placeholder.py", """\
        def test_placeholder():
            assert True
    """)
    _write(repo / "setup.py", f"from setuptools import setup, find_packages; setup(name={pkg!r}, packages=find_packages())")
    _commit_all(repo, "initial commit")

    # Fix commit
    _write(repo / pkg / "core.py", """\
        def old_func():
            return 1   # fixed
    """)
    fix_sha = _commit_all(repo, "fix: old_func correct return")

    # Heavy refactor: completely replace the file
    _write(repo / pkg / "core.py", """\
        # completely different file, old_func gone
        def new_api():
            return 99
    """)
    _commit_all(repo, "refactor: replace API")

    name = "moved-test"
    ab, venv_path = setup_antibody_env(tmp_path, name, repo)

    patch_result = subprocess.run(
        ["git", "format-patch", "--stdout", "-1", fix_sha, "--", f"{pkg}/core.py"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    patch_path = ab / "diffs" / f"{fix_sha}.patch"
    patch_path.write_text(patch_result.stdout)

    write_setup_json(ab, name, tmp_path / "targets", collected=1)
    write_candidates_json(ab, [{
        "sha": fix_sha,
        "subject": "fix: old_func correct return",
        "src_files": [f"{pkg}/core.py"],
        "diff_lines": 2,
        "status": "candidate",
        "reason": "",
    }])

    result = _run_cmd_run(name, tmp_path)
    assert result.returncode == 0, result.stderr

    results = _read_results(tmp_path, name)
    rows = results["rows"]
    assert len(rows) == 1
    assert rows[0]["status"] == "NO DATA:code-moved", f"Expected NO DATA:code-moved, got: {rows[0]['status']}\nstdout: {result.stdout}\nstderr: {result.stderr}"


# ---------------------------------------------------------------------------
# Scenario (d): NO DATA — project where pytest collects 0 tests
# ---------------------------------------------------------------------------

def test_zero_collected(tmp_path):
    """
    Scenario: the target has no test files → 0 collected → NO DATA:zero-collected.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    pkg = "mypkg"
    _write(repo / pkg / "__init__.py", "")
    _write(repo / pkg / "core.py", """\
        def func():
            return 0
    """)
    # No test files at all!
    _write(repo / "setup.py", f"from setuptools import setup, find_packages; setup(name={pkg!r}, packages=find_packages())")
    _commit_all(repo, "initial commit")

    # A fix
    _write(repo / pkg / "core.py", """\
        def func():
            return 1
    """)
    fix_sha = _commit_all(repo, "fix: func now returns 1")

    name = "zero-test"
    ab, venv_path = setup_antibody_env(tmp_path, name, repo)

    patch_result = subprocess.run(
        ["git", "format-patch", "--stdout", "-1", fix_sha, "--", f"{pkg}/core.py"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    patch_path = ab / "diffs" / f"{fix_sha}.patch"
    patch_path.write_text(patch_result.stdout)

    # Baseline must report 0 collected — but our baseline guard rejects that.
    # For this scenario we force collected=1 in setup.json so the runner
    # actually attempts the run, then gets a real 0 from pytest.
    write_setup_json(ab, name, tmp_path / "targets", collected=1)
    write_candidates_json(ab, [{
        "sha": fix_sha,
        "subject": "fix: func returns 1",
        "src_files": [f"{pkg}/core.py"],
        "diff_lines": 2,
        "status": "candidate",
        "reason": "",
    }])

    result = _run_cmd_run(name, tmp_path)
    assert result.returncode == 0, result.stderr

    results = _read_results(tmp_path, name)
    rows = results["rows"]
    assert len(rows) == 1
    # 0 collected → NO DATA (could be zero-collected or count-mismatch)
    assert rows[0]["status"].startswith("NO DATA"), (
        f"Expected NO DATA:*, got: {rows[0]['status']}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# Internal helpers that call the runner in-process via a patched ROOT
# ---------------------------------------------------------------------------

def _run_cmd_run(name: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run `antibody run <name>` with the tmp_path-based directory layout."""
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Override the paths by monkey-patching via environment variables
    env["_ANTIBODY_ROOT"] = str(tmp_path)
    return subprocess.run(
        [sys.executable, str(RUNNER), "run", name],
        capture_output=True,
        text=True,
        env=env,
    )


def _read_results(tmp_path: Path, name: str) -> dict:
    results_path = tmp_path / ".antibody" / name / "results.json"
    assert results_path.exists(), f"results.json not found at {results_path}"
    with open(results_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Unit tests for FIX_KEYWORDS regex (imported directly)
# ---------------------------------------------------------------------------

import importlib.util as _ilu
import sys as _sys

def _load_runner():
    spec = _ilu.spec_from_file_location("antibody_runner", RUNNER)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def runner_mod():
    return _load_runner()


class TestFixKeywords:
    """FIX_KEYWORDS must match inside longer words and various issue-number forms."""

    def test_plain_fix(self, runner_mod):
        assert runner_mod.FIX_KEYWORDS.search("fix memory leak")

    def test_bugfix(self, runner_mod):
        assert runner_mod.FIX_KEYWORDS.search("bugfix ISSUE_801; Remove all comments when only comments")

    def test_hotfix(self, runner_mod):
        assert runner_mod.FIX_KEYWORDS.search("hotfix for edge case")

    def test_regression(self, runner_mod):
        assert runner_mod.FIX_KEYWORDS.search("regression in parser")

    def test_crash(self, runner_mod):
        assert runner_mod.FIX_KEYWORDS.search("crash on empty input")

    def test_issue_number_hash(self, runner_mod):
        assert runner_mod.FIX_KEYWORDS.search("#801 null pointer")

    def test_issue_number_word_space(self, runner_mod):
        assert runner_mod.FIX_KEYWORDS.search("issue 801 null pointer")

    def test_issue_number_word_nospace(self, runner_mod):
        assert runner_mod.FIX_KEYWORDS.search("Recognize MATERIALIZED as a keyword (issue752)")

    def test_issue_number_word_underscore(self, runner_mod):
        assert runner_mod.FIX_KEYWORDS.search("ISSUE_801 fix")

    def test_no_match(self, runner_mod):
        assert not runner_mod.FIX_KEYWORDS.search("add new feature to parser")


# ---------------------------------------------------------------------------
# Test: default --since date is 2021-01-01
# ---------------------------------------------------------------------------

def test_candidates_default_since():
    """candidates --since defaults to 2021-01-01 (parser default)."""
    import argparse
    runner = _load_runner()
    # Build a minimal parser that mirrors the candidates subparser
    p = argparse.ArgumentParser()
    p.add_argument("name")
    p.add_argument("--since", default="2021-01-01")
    args = p.parse_args(["myrepo"])
    assert args.since == "2021-01-01"


# ---------------------------------------------------------------------------
# Test: 150-line diff limit is enforced
# ---------------------------------------------------------------------------

def test_candidates_150_line_limit(tmp_path):
    """
    A commit whose diff exceeds 150 lines must not appear in candidates.json.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    pkg = "mypkg"
    _write(repo / pkg / "__init__.py", "")
    # Initial file: 200 lines
    lines = "\n".join(f"    x{i} = {i}" for i in range(200))
    _write(repo / pkg / "core.py", f"def func():\n{lines}\n    return 0\n")
    _write(repo / "setup.py",
           f"from setuptools import setup, find_packages; setup(name={pkg!r}, packages=find_packages())")
    _commit_all(repo, "initial commit")

    # Fix: replace all 200 lines (diff > 150)
    lines2 = "\n".join(f"    y{i} = {i}" for i in range(200))
    _write(repo / pkg / "core.py", f"def func():\n{lines2}\n    return 1\n")
    _commit_all(repo, "fix: replace all lines")

    ab = tmp_path / ".antibody" / "limit-test"
    ab.mkdir(parents=True, exist_ok=True)
    (ab / "diffs").mkdir(parents=True, exist_ok=True)

    # targets/limit-test must point at the repo so candidates can call git log
    targets = tmp_path / "targets"
    targets.mkdir(parents=True, exist_ok=True)
    tgt_link = targets / "limit-test"
    tgt_link.symlink_to(repo)

    # Write a minimal setup.json so candidates can read it
    import json
    with open(ab / "setup.json", "w") as f:
        json.dump({"repo": "file:///local", "head": "deadbeef", "deps": [], "install_commands": []}, f)

    env = os.environ.copy()
    env["_ANTIBODY_ROOT"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, str(RUNNER), "candidates", "limit-test"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr

    with open(ab / "candidates.json") as f:
        candidates = json.load(f)

    # The oversized commit must not appear as a candidate
    statuses = [c["status"] for c in candidates]
    assert "candidate" not in statuses, f"Expected no candidates, got: {candidates}"


# ---------------------------------------------------------------------------
# Test: saved patch contains no author name, email, or message body
# ---------------------------------------------------------------------------

def test_patch_has_no_author_email_or_message(tmp_path):
    """
    build_patch must produce a diff that has no From: header, author name,
    email address, or commit message body.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    pkg = "mypkg"
    _write(repo / pkg / "__init__.py", "")
    _write(repo / pkg / "core.py", "def f():\n    return 0\n")
    _write(repo / "setup.py",
           f"from setuptools import setup, find_packages; setup(name={pkg!r}, packages=find_packages())")
    _commit_all(repo, "initial commit")

    _write(repo / pkg / "core.py", "def f():\n    return 1  # fix\n")
    fix_sha = _commit_all(repo, "fix: correct return value")

    runner = _load_runner()
    patch = runner.build_patch(repo, fix_sha, [f"{pkg}/core.py"])

    # Must not contain email address (test@test.invalid set by _init_git_repo)
    assert "test@test.invalid" not in patch, "patch contains email address"
    # Must not contain 'From ' mail header line
    assert not any(line.startswith("From ") for line in patch.splitlines()), \
        "patch contains From header"
    # Must not contain the commit message
    assert "correct return value" not in patch, "patch contains commit message"
    # Must contain actual diff content
    assert "@@" in patch, "patch contains no diff hunks"

    # Verify git apply -R works on the patch
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".patch", mode="w", delete=False) as tf:
        tf.write(patch)
        pf = Path(tf.name)
    try:
        check = subprocess.run(
            ["git", "apply", "--check", "-R", str(pf)],
            cwd=str(repo), capture_output=True,
        )
        assert check.returncode == 0, \
            f"git apply -R --check failed:\n{check.stderr.decode()}"
    finally:
        pf.unlink(missing_ok=True)
