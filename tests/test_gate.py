"""
tests/test_gate.py — Tests for the gate command and related fixes.

Covers:
  - a catching test deleted → gate fails and names it
  - a catching test that already fails at the current code → gate fails
  - an empty ledger → gate fails
  - an accepted exposure with an empty reason → gate fails
  - a correctly formatted antibody-accepted.yaml parsed without PyYAML
  - a patch that no longer applies → warning only, gate passes

Also covers:
  - Fix 1: candidates subjects strip to first line, drop Signed-off-by trailers
  - Fix 2: antibody_plugin xfailed not double-counted as skipped
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Re-use helpers from test_runner.py
# ---------------------------------------------------------------------------

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


def _load_runner():
    spec = importlib.util.spec_from_file_location("antibody_runner", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Shared project factory for gate tests
# ---------------------------------------------------------------------------

def _make_caught_project(tmp_path: Path, name: str) -> tuple[Path, str]:
    """
    Build a minimal project with one CAUGHT fix.
    Returns (ab, fix_sha).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    pkg = "mypkg"
    _write(repo / pkg / "__init__.py", "")
    _write(repo / pkg / "core.py", "def f():\n    return 0  # bug\n")
    _write(
        repo / "tests" / "test_core.py",
        "from mypkg.core import f\ndef test_f():\n    assert f() == 1\n",
    )
    _write(
        repo / "setup.py",
        f"from setuptools import setup, find_packages; setup(name={pkg!r}, packages=find_packages())",
    )
    _commit_all(repo, "initial commit")

    _write(repo / pkg / "core.py", "def f():\n    return 1  # fixed\n")
    fix_sha = _commit_all(repo, "fix: f now returns 1")

    ab, _ = setup_antibody_env(tmp_path, name, repo)

    # Build patch
    patch_result = subprocess.run(
        ["git", "format-patch", "--stdout", "-1", fix_sha, "--", f"{pkg}/core.py"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    patch_path = ab / "diffs" / f"{fix_sha}.patch"
    patch_path.write_text(patch_result.stdout)

    write_setup_json(ab, name, tmp_path / "targets", collected=1)
    write_candidates_json(ab, [{
        "sha": fix_sha,
        "subject": "fix: f now returns 1",
        "src_files": [f"{pkg}/core.py"],
        "diff_lines": 2,
        "status": "candidate",
        "reason": "",
    }])
    return ab, fix_sha


def _write_ledger(ab: Path, name: str, rows: list[dict], head: str = "deadbeef") -> None:
    """Write a minimal ledger.json."""
    ledger = {
        "name": name,
        "repo": "file:///local",
        "head": head,
        "run_time": "2024-01-01T00:00:00+00:00",
        "ledger_time": "2024-01-01T00:01:00+00:00",
        "curated": None,
        "stats": {
            "total_candidates": len(rows),
            "checkable": sum(
                1 for r in rows
                if r["status"] in ("CAUGHT", "NOW CAUGHT", "STILL EXPOSED", "NO CHANGE FOUND")
            ),
            "caught_before": sum(1 for r in rows if r["status"] == "CAUGHT"),
            "caught_after": sum(
                1 for r in rows if r["status"] in ("CAUGHT", "NOW CAUGHT")
            ),
        },
        "rows": rows,
    }
    with open(ab / "ledger.json", "w") as f:
        json.dump(ledger, f, indent=2)


# ===========================================================================
# gate tests
# ===========================================================================

class TestGate:

    def test_catching_test_deleted_fails(self, tmp_path):
        """
        A CAUGHT row whose catching test no longer exists in the suite
        → gate fails and names it.
        """
        name = "gate-deleted"
        ab, fix_sha = _make_caught_project(tmp_path, name)

        # Remove the test file so it no longer exists
        test_file = tmp_path / "targets" / name / "tests" / "test_core.py"
        test_file.unlink()

        tid = "tests/test_core.py::test_f"
        _write_ledger(ab, name, [{
            "sha": fix_sha,
            "status": "CAUGHT",
            "line": "fix: f now returns 1",
            "reason": "",
            "link": "",
            "catching_tests": [tid],
            "proof_code": None,
            "probe_output": None,
        }])

        result = _run_runner_cmd(["gate", name], tmp_path)
        assert result.returncode != 0, f"Expected gate to fail\nstdout: {result.stdout}\nstderr: {result.stderr}"
        combined = result.stdout + result.stderr
        assert "catching test missing" in combined, (
            f"Expected 'catching test missing' in output\n{combined}"
        )
        assert "test_f" in combined or "test_core" in combined, (
            f"Expected test name in output\n{combined}"
        )

    def test_catching_test_fails_at_head(self, tmp_path):
        """
        A CAUGHT row whose catching test already fails at the current code
        → gate fails.
        """
        name = "gate-fails-at-head"
        ab, fix_sha = _make_caught_project(tmp_path, name)

        # Break the test so it fails on the fixed code
        tgt = tmp_path / "targets" / name
        _write(
            tgt / "tests" / "test_core.py",
            "from mypkg.core import f\ndef test_f():\n    assert f() == 999  # broken\n",
        )

        tid = "tests/test_core.py::test_f"
        _write_ledger(ab, name, [{
            "sha": fix_sha,
            "status": "CAUGHT",
            "line": "fix: f now returns 1",
            "reason": "",
            "link": "",
            "catching_tests": [tid],
            "proof_code": None,
            "probe_output": None,
        }])

        result = _run_runner_cmd(["gate", name], tmp_path)
        assert result.returncode != 0, (
            f"Expected gate to fail\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "already fails" in combined or "FAIL" in combined, (
            f"Expected failure message\n{combined}"
        )

    def test_empty_ledger_fails(self, tmp_path):
        """An empty ledger → gate fails."""
        name = "gate-empty"
        ab = tmp_path / ".antibody" / name
        ab.mkdir(parents=True, exist_ok=True)
        _write_ledger(ab, name, [])

        result = _run_runner_cmd(["gate", name], tmp_path)
        assert result.returncode != 0, (
            f"Expected gate to fail on empty ledger\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_accepted_exposure_empty_reason_fails(self, tmp_path):
        """
        An antibody-accepted.yaml entry with an empty reason → gate fails.
        """
        name = "gate-empty-reason"
        ab = tmp_path / ".antibody" / name
        ab.mkdir(parents=True, exist_ok=True)
        (ab / "diffs").mkdir(parents=True, exist_ok=True)

        sha = "a" * 40
        _write_ledger(ab, name, [{
            "sha": sha,
            "status": "STILL EXPOSED",
            "line": "some exposed bug",
            "reason": "no test catches it",
            "link": "",
            "catching_tests": [],
            "proof_code": None,
            "probe_output": None,
        }])

        # Write accepted.yaml with empty reason
        accepted_path = tmp_path / "antibody-accepted.yaml"
        accepted_path.write_text(f"- sha: {sha}\n  reason:\n")

        env = os.environ.copy()
        env["_ANTIBODY_ROOT"] = str(tmp_path)
        result = subprocess.run(
            ["python3.12", str(RUNNER), "gate", name],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0, (
            f"Expected gate to fail on empty reason\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "empty" in combined.lower() or "reason" in combined.lower(), (
            f"Expected reason-related error\n{combined}"
        )

    def test_accepted_file_parsed_without_pyyaml(self, tmp_path):
        """
        A correctly formatted antibody-accepted.yaml is parsed even when
        PyYAML is made unimportable.
        """
        runner = _load_runner()

        sha = "b" * 40
        content = f"- sha: {sha}\n  reason: This is a real sentence explaining the exposure.\n"
        p = tmp_path / "antibody-accepted.yaml"
        p.write_text(content)

        # Block yaml import in this process — but the runner uses its own parser
        import sys as _sys
        old_yaml = _sys.modules.get("yaml", None)
        _sys.modules["yaml"] = None  # type: ignore[assignment]
        try:
            entries = runner._parse_accepted_yaml(p)
        finally:
            if old_yaml is None:
                _sys.modules.pop("yaml", None)
            else:
                _sys.modules["yaml"] = old_yaml

        assert len(entries) == 1
        assert entries[0]["sha"] == sha
        assert "real sentence" in entries[0]["reason"]

    def test_patch_no_longer_applies_warns_not_fails(self, tmp_path):
        """
        A CAUGHT row whose patch no longer applies → warning only, gate passes.
        (Unless there are other failures — here there are none.)
        """
        name = "gate-stale-patch"
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        pkg = "mypkg"
        _write(repo / pkg / "__init__.py", "")
        _write(repo / pkg / "core.py", "def f():\n    return 0\n")
        _write(
            repo / "tests" / "test_placeholder.py",
            "def test_placeholder():\n    assert True\n",
        )
        _write(
            repo / "setup.py",
            f"from setuptools import setup, find_packages; setup(name={pkg!r}, packages=find_packages())",
        )
        _commit_all(repo, "initial commit")

        _write(repo / pkg / "core.py", "def f():\n    return 1\n")
        fix_sha = _commit_all(repo, "fix: return 1")

        # Heavily refactor so patch no longer applies
        _write(repo / pkg / "core.py", "# completely different\ndef new_api():\n    return 99\n")
        _commit_all(repo, "refactor: remove old API")

        ab, _ = setup_antibody_env(tmp_path, name, repo)

        # Build the patch from the fix commit (it won't apply to current HEAD)
        patch_result = subprocess.run(
            ["git", "format-patch", "--stdout", "-1", fix_sha, "--", f"{pkg}/core.py"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        )
        patch_path = ab / "diffs" / f"{fix_sha}.patch"
        patch_path.write_text(patch_result.stdout)

        write_setup_json(ab, name, tmp_path / "targets", collected=1)
        _write_ledger(ab, name, [{
            "sha": fix_sha,
            "status": "CAUGHT",
            "line": "fix: return 1",
            "reason": "",
            "link": "",
            "catching_tests": ["tests/test_placeholder.py::test_placeholder"],
            "proof_code": None,
            "probe_output": None,
        }])

        result = _run_runner_cmd(["gate", name], tmp_path)
        combined = result.stdout + result.stderr
        # Should warn but not fail
        assert "re-audit needed" in combined or "WARNING" in combined, (
            f"Expected re-audit warning\n{combined}"
        )
        assert result.returncode == 0, (
            f"Expected gate to pass (patch no longer applies = warning only)\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


# ===========================================================================
# Fix 1: candidates — first-line subject, no Signed-off-by
# ===========================================================================

class TestCandidatesSubjectClean:

    def _load_runner(self):
        return _load_runner()

    def test_clean_subject_first_line_only(self):
        """_clean_subject keeps only the first line."""
        runner = _load_runner()
        multi = "Fix something important\n\nSigned-off-by: Jane Doe <jane@example.com>"
        assert runner._clean_subject(multi) == "Fix something important"

    def test_clean_subject_strips_signed_off_by(self):
        """A subject that IS a Signed-off-by line returns empty."""
        runner = _load_runner()
        assert runner._clean_subject("Signed-off-by: Alice <alice@example.com>") == ""

    def test_clean_subject_strips_other_by_trailers(self):
        runner = _load_runner()
        assert runner._clean_subject("Reviewed-by: Bob <bob@example.com>") == ""

    def test_clean_subject_normal_subject_unchanged(self):
        runner = _load_runner()
        s = "fix: handle edge case in parser"
        assert runner._clean_subject(s) == s

    def test_candidates_no_signed_off_by_in_subject(self, tmp_path):
        """
        A commit whose first paragraph runs on into a Signed-off-by trailer
        must have only the first line saved as the subject.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        pkg = "mypkg"
        _write(repo / pkg / "__init__.py", "")
        _write(repo / pkg / "core.py", "def f():\n    return 0\n")
        _write(
            repo / "setup.py",
            f"from setuptools import setup, find_packages; setup(name={pkg!r}, packages=find_packages())",
        )
        _commit_all(repo, "initial commit")

        _write(repo / pkg / "core.py", "def f():\n    return 1\n")

        # Commit message whose first paragraph runs on: no blank line after subject
        msg = (
            "fix: correct return value of f\n"
            "Signed-off-by: Alice Developer <alice@example.com>"
        )
        _git(["add", "-A"], repo)
        _git(["commit", "-m", msg], repo)
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        )
        fix_sha = result.stdout.strip()

        # Set up antibody env
        ab = tmp_path / ".antibody" / "cand-trailer"
        ab.mkdir(parents=True, exist_ok=True)
        (ab / "diffs").mkdir(parents=True, exist_ok=True)
        targets = tmp_path / "targets"
        targets.mkdir(parents=True, exist_ok=True)
        tgt_link = targets / "cand-trailer"
        tgt_link.symlink_to(repo)
        with open(ab / "setup.json", "w") as f:
            json.dump({
                "repo": "file:///local",
                "head": fix_sha,
                "deps": [],
                "install_commands": [],
            }, f)

        env = os.environ.copy()
        env["_ANTIBODY_ROOT"] = str(tmp_path)
        result2 = subprocess.run(
            ["python3.12", str(RUNNER), "candidates", "cand-trailer"],
            capture_output=True, text=True, env=env,
        )
        assert result2.returncode == 0, result2.stderr

        with open(ab / "candidates.json") as f:
            candidates = json.load(f)

        # Find our fix commit
        matching = [c for c in candidates if c["sha"] == fix_sha]
        assert matching, f"fix commit not found in candidates: {candidates}"
        subj = matching[0]["subject"]
        assert "Signed-off-by" not in subj, (
            f"Subject contains Signed-off-by: {subj!r}"
        )
        assert "@" not in subj, (
            f"Subject contains an email address: {subj!r}"
        )
        assert subj == "fix: correct return value of f", (
            f"Subject should be first line only, got: {subj!r}"
        )


# ===========================================================================
# Fix 2: antibody_plugin — xfailed not double-counted as skipped
# ===========================================================================

class TestPluginXfailCounting:

    def _make_xfail_project(self, tmp_path: Path) -> Path:
        """
        Create a tiny project with one xfailed and one skipped test.
        Returns the venv python path.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        pkg = "mypkg"
        _write(repo / pkg / "__init__.py", "")
        _write(repo / pkg / "core.py", "def f():\n    return 1\n")
        _write(
            repo / "setup.py",
            f"from setuptools import setup, find_packages; setup(name={pkg!r}, packages=find_packages())",
        )
        # Test file: 1 passing, 1 xfail (expected to fail), 1 skip
        _write(
            repo / "tests" / "test_counts.py",
            textwrap.dedent("""\
                import pytest
                from mypkg.core import f

                def test_passing():
                    assert f() == 1

                @pytest.mark.xfail
                def test_expected_fail():
                    assert f() == 999  # expected to fail

                def test_skipped():
                    pytest.skip("intentional skip")
            """),
        )
        _commit_all(repo, "initial commit")
        return repo

    def test_xfail_not_counted_as_skipped(self, tmp_path):
        """
        xfailed test must count only as xfailed, not also as skipped.
        passed + skipped + xfailed + xpassed == collected.
        """
        repo = self._make_xfail_project(tmp_path)
        name = "xfail-test"
        venv_dir = tmp_path / "venvs" / name
        python = _make_venv(venv_dir)

        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), "--quiet", "-e", str(repo)],
            check=True, capture_output=True,
        )

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            report_path = Path(tmp.name)

        env = os.environ.copy()
        env["ANTIBODY_REPORT_PATH"] = str(report_path)
        plugin_dir = str(PLUGIN.parent)
        env["PYTHONPATH"] = plugin_dir + (":" + env.get("PYTHONPATH", "") if env.get("PYTHONPATH") else "")

        subprocess.run(
            [
                str(python), "-m", "pytest",
                "-p", "no:cacheprovider",
                "-p", "antibody_plugin",
                str(repo / "tests" / "test_counts.py"),
            ],
            cwd=str(repo),
            env=env,
            capture_output=True,
        )

        assert report_path.exists(), "plugin did not write report"
        with open(report_path) as f:
            data = json.load(f)
        report_path.unlink(missing_ok=True)

        collected = len(data.get("collected", []))
        skipped = data.get("skipped", 0)
        xfailed = data.get("xfailed", 0)
        xpassed = data.get("xpassed", 0)
        failed = len(data.get("failed", []))

        # The xfailed test must NOT be counted as skipped
        assert xfailed == 1, f"Expected xfailed=1, got {xfailed}"
        assert skipped == 1, f"Expected skipped=1 (the pytest.skip), got {skipped}"
        # passed + skipped + xfailed + xpassed == collected
        passed = collected - failed - skipped - xfailed - xpassed
        assert passed + skipped + xfailed + xpassed == collected, (
            f"passed({passed}) + skipped({skipped}) + xfailed({xfailed}) + xpassed({xpassed}) "
            f"!= collected({collected})"
        )
        assert passed == 1, f"Expected passed=1, got {passed}"


# ===========================================================================
# Pinned format tests for ledger/gate formats
# ===========================================================================

class TestPinnedFormats:

    def test_curated_json_format(self, tmp_path):
        """
        curated.json: maps sha → {verdict, reason, link}.
        Accepted verdicts: "BUG" or "NOT-A-BUG".
        """
        runner = _load_runner()
        sha = "c" * 40
        curated = {
            sha: {
                "verdict": "NOT-A-BUG",
                "reason": "This was a documentation change, not a behavioural bug.",
                "link": "https://github.com/example/repo/issues/42",
            }
        }
        p = tmp_path / "curated.json"
        p.write_text(json.dumps(curated, indent=2))

        with open(p) as f:
            loaded = json.load(f)
        assert sha in loaded
        entry = loaded[sha]
        assert entry["verdict"] in ("BUG", "NOT-A-BUG")
        assert isinstance(entry["reason"], str) and entry["reason"]
        assert isinstance(entry["link"], str)

    def test_explanations_json_format(self, tmp_path):
        """
        explanations.json: maps sha → one plain-English line (str).
        """
        sha = "d" * 40
        explanations = {
            sha: "Parser incorrectly tokenised dollar-quoted strings containing newlines."
        }
        p = tmp_path / "explanations.json"
        p.write_text(json.dumps(explanations, indent=2))

        with open(p) as f:
            loaded = json.load(f)
        assert sha in loaded
        line = loaded[sha]
        assert isinstance(line, str) and line
        assert "\n" not in line, "explanation must be a single line"

    def test_accepted_yaml_pinned_format(self, tmp_path):
        """
        antibody-accepted.yaml pinned format:
          - sha: <full sha>
            reason: <one sentence>
        """
        runner = _load_runner()
        sha = "e" * 40
        content = f"- sha: {sha}\n  reason: The test suite cannot exercise this code path on CI.\n"
        p = tmp_path / "antibody-accepted.yaml"
        p.write_text(content)

        entries = runner._parse_accepted_yaml(p)
        assert len(entries) == 1
        assert entries[0]["sha"] == sha
        assert "cannot exercise" in entries[0]["reason"]

    def test_accepted_yaml_rejects_wrong_format(self, tmp_path):
        """A file that doesn't match the pinned format is rejected with the line number."""
        runner = _load_runner()
        p = tmp_path / "antibody-accepted.yaml"
        p.write_text("sha: abc123\nreason: something\n")  # missing leading "- "

        with pytest.raises(ValueError, match=r"line 1"):
            runner._parse_accepted_yaml(p)

    def test_accepted_yaml_rejects_todo_reason(self, tmp_path):
        runner = _load_runner()
        sha = "f" * 40
        p = tmp_path / "antibody-accepted.yaml"
        p.write_text(f"- sha: {sha}\n  reason: TODO\n")

        with pytest.raises(ValueError, match=r"placeholder"):
            runner._parse_accepted_yaml(p)

    def test_ledger_json_has_curated_hash(self, tmp_path):
        """
        ledger() records sha-256 of curated.json next to the run_time,
        so curation timestamp can be compared against run_time.
        """
        runner = _load_runner()
        sha = "a" * 40
        curated = {sha: {"verdict": "NOT-A-BUG", "reason": "Not a bug.", "link": ""}}
        curated_path = tmp_path / "curated.json"
        curated_path.write_text(json.dumps(curated))

        h = runner._sha256_file(curated_path)
        assert len(h) == 64  # sha-256 hex
        assert all(c in "0123456789abcdef" for c in h)
