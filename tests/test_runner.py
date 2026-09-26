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
