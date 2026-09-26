"""
tests/test_ledger.py — ledger with curation, `run --shas`, and new tests.

Covers:
  - a curated NOT-A-BUG verdict (the pinned format) gives NOT A BUG, outside Y
  - ledger after `run --shas`: candidates with no results row are fine when
    curated NOT-A-BUG or excluded; any other missing row names the candidate
  - a one-line subject that runs on into a trailer loses the trailer and email
  - prove accepts the test path from the repository root, as the skill passes it,
    and records it; ledger re-proves it and the row becomes NOW CAUGHT
"""

from __future__ import annotations

import importlib.util
import json
import textwrap
from pathlib import Path

_PP_PATH = Path(__file__).parent / "test_probe_prove.py"
_spec = importlib.util.spec_from_file_location("_test_probe_prove_helpers", _PP_PATH)
_pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pp)

_commit_all = _pp._commit_all
_make_project = _pp._make_project
_run_runner_cmd = _pp._run_runner_cmd
_setup_target = _pp._setup_target
_write = _pp._write
write_candidates_json = _pp.write_candidates_json
write_setup_json = _pp.write_setup_json
RUNNER = _pp.RUNNER


def _load_runner():
    spec = importlib.util.spec_from_file_location("antibody_runner_ledger", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cand(sha: str, status: str = "candidate") -> dict:
    return {"sha": sha, "subject": f"fix {sha[:4]}", "src_files": ["pkg/core.py"],
            "diff_lines": 3, "status": status, "reason": ""}


def _result(sha: str, status: str, catching: list[str] | None = None) -> dict:
    return {"sha": sha, "subject": f"fix {sha[:4]}", "src_files": ["pkg/core.py"],
            "status": status, "reason": "", "catching_tests": catching or [], "secs": 0.1}


def _write_ledger_inputs(tmp_path: Path, name: str, candidates: list[dict],
                         rows: list[dict], curated: dict) -> Path:
    ab = tmp_path / ".antibody" / name
    ab.mkdir(parents=True)
    write_setup_json(ab, name, tmp_path / "targets", collected=1)
    write_candidates_json(ab, candidates)
    (ab / "results.json").write_text(json.dumps({"rows": rows}))
    (ab / "curated.json").write_text(json.dumps(curated))
    return ab


def _curated(verdict: str) -> dict:
    return {"verdict": verdict, "reason": "Reason quoting the thread.",
            "link": "https://github.com/example/repo/issues/1"}


class TestLedgerCuration:

    def test_not_a_bug_verdict_gives_not_a_bug(self):
        runner = _load_runner()
        sha = "a" * 40
        row = _result(sha, "ESCAPED")
        lr = runner._map_status(row, None, None, {sha: _curated("NOT-A-BUG")}, {})
        assert lr["status"] == "NOT A BUG"
        lr = runner._map_status(row, None, None, {sha: _curated("BUG")}, {})
        assert lr["status"] == "STILL EXPOSED"

    def test_ledger_after_run_shas(self, tmp_path):
        """Only BUG rows were run; NOT-A-BUG and excluded candidates still get a row."""
        caught, exposed, nab_run, nab_not_run, excluded = (c * 40 for c in "abcde")
        candidates = [_cand(caught), _cand(exposed), _cand(nab_run), _cand(nab_not_run),
                      _cand(excluded, "EXCLUDED:non-behavioural")]
        rows = [_result(caught, "CAUGHT", ["tests/test_core.py::test_x"]),
                _result(exposed, "ESCAPED"),
                _result(nab_run, "CAUGHT", ["tests/test_core.py::test_y"])]
        curated = {caught: _curated("BUG"), exposed: _curated("BUG"),
                   nab_run: _curated("NOT-A-BUG"), nab_not_run: _curated("NOT-A-BUG")}
        ab = _write_ledger_inputs(tmp_path, "cur", candidates, rows, curated)

        result = _run_runner_cmd(["ledger", "cur"], tmp_path)
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"

        ledger = json.loads((ab / "ledger.json").read_text())
        statuses = [(r["sha"], r["status"]) for r in ledger["rows"]]
        assert statuses == [(caught, "CAUGHT"), (exposed, "STILL EXPOSED"),
                            (nab_run, "NOT A BUG"), (nab_not_run, "NOT A BUG"),
                            (excluded, "EXCLUDED")]
        assert ledger["stats"]["total_candidates"] == 5
        assert ledger["stats"]["checkable"] == 2
        assert ledger["stats"]["caught_before"] == 1
        assert ledger["stats"]["caught_after"] == 1

    def test_ledger_names_candidate_without_results_row(self, tmp_path):
        run_row, forgotten = "a" * 40, "f" * 40
        _write_ledger_inputs(
            tmp_path, "miss",
            [_cand(run_row), _cand(forgotten)],
            [_result(run_row, "CAUGHT", ["tests/test_core.py::test_x"])],
            {run_row: _curated("BUG"), forgotten: _curated("BUG")},
        )
        result = _run_runner_cmd(["ledger", "miss"], tmp_path)
        assert result.returncode != 0
        assert forgotten[:12] in result.stdout + result.stderr


class TestSubjectInlineTrailer:

    def test_one_line_subject_running_into_signed_off_by(self):
        runner = _load_runner()
        raw = ("fix: -1 index error when setting dotted keyFixes #332"
               "Signed-off-by: Jane Doe <jane.doe@example.com>")
        assert runner._clean_subject(raw) == "fix: -1 index error when setting dotted keyFixes #332"

    def test_email_removed_and_other_colons_kept(self):
        runner = _load_runner()
        assert runner._clean_subject("Merge fix from jane.doe@example.com") == "Merge fix from"
        assert runner._clean_subject("Fix build with gcc: use -O2") == "Fix build with gcc: use -O2"
        assert runner._clean_subject("fix parser Reviewed-By: J D <j@example.com>") == "fix parser"


class TestNowCaught:

    def test_new_test_proved_then_now_caught_and_published(self, tmp_path):
        buggy = "def add(a, b):\n    return a - b  # BUG\n"
        fixed = "def add(a, b):\n    return a + b  # FIXED\n"
        other_src = "def test_other():\n    assert True\n"
        repo, _, fix_sha = _make_project(tmp_path, buggy, fixed, test_src=other_src)

        name = "nowcaught"
        ab = _setup_target(tmp_path, name, repo, fix_sha)
        (ab / "results.json").write_text(json.dumps({"rows": [
            {"sha": fix_sha, "subject": "fix: correct behaviour", "src_files": ["mypkg/core.py"],
             "status": "ESCAPED", "reason": "", "catching_tests": [], "secs": 0.1},
        ]}))

        tests_ab = tmp_path / "targets" / name / "tests" / "antibody"
        _write(tests_ab / "test_fix.py", textwrap.dedent("""\
            from mypkg.core import add
            def test_add():
                assert add(2, 3) == 5
        """))

        # The skill passes the test as a path from the repository root.
        result = _run_runner_cmd(
            ["prove", name, fix_sha, f"targets/{name}/tests/antibody/test_fix.py"], tmp_path)
        assert "PROVEN" in result.stdout and "NOT PROVEN" not in result.stdout, (
            f"stdout: {result.stdout}\nstderr: {result.stderr}")
        proof = json.loads((ab / "proofs" / f"{fix_sha}.json").read_text())
        assert proof["verdict"] == "PROVEN"
        assert proof["test"] == "tests/antibody/test_fix.py"
        # Putting the bug back and restoring the tree must keep the new test.
        assert (tests_ab / "test_fix.py").exists()

        # A test that never proved anything must not be published.
        _write(tests_ab / "test_unproven.py", "def test_nothing():\n    assert True\n")

        result = _run_runner_cmd(["ledger", name, "--publish"], tmp_path)
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
        ledger = json.loads((ab / "ledger.json").read_text())
        (row,) = ledger["rows"]
        assert row["status"] == "NOW CAUGHT", result.stdout
        assert row["catching_tests"] == ["tests/antibody/test_fix.py"]
        assert ledger["stats"]["caught_before"] == 0
        assert ledger["stats"]["caught_after"] == 1
        assert (tmp_path / "audits" / name / "test_fix.py").exists()
        assert not (tmp_path / "audits" / name / "test_unproven.py").exists()
