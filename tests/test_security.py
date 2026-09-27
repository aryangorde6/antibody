"""
Argument checks and ledger links (hand-written after the task 07 review).

A target name or sha becomes part of a path under targets/ and .antibody/, so it
must not climb out of them; a URL or --rev starting with '-' would be read as a
git option; and a row's issue link comes from Bob's curation, so only https
links may become clickable on the published page.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PP_PATH = Path(__file__).parent / "test_probe_prove.py"
_spec = importlib.util.spec_from_file_location("_test_probe_prove_helpers_sec", _PP_PATH)
_pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pp)

_run_runner_cmd = _pp._run_runner_cmd
RUNNER = _pp.RUNNER


def _load_runner():
    spec = importlib.util.spec_from_file_location("antibody_runner_security", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_name_that_climbs_out_is_rejected(tmp_path):
    result = _run_runner_cmd(["gate", ".."], tmp_path)
    assert result.returncode != 0
    assert "invalid target name" in result.stderr
    assert not (tmp_path / ".antibody").exists()


def test_name_with_a_slash_is_rejected(tmp_path):
    result = _run_runner_cmd(["ledger", "a/../../b"], tmp_path)
    assert result.returncode != 0
    assert "invalid target name" in result.stderr


def test_sha_with_a_path_is_rejected(tmp_path):
    result = _run_runner_cmd(["prove", "demo", "../../x", "tests/test_x.py"], tmp_path)
    assert result.returncode != 0
    assert "invalid sha" in result.stderr


def test_url_starting_with_a_dash_is_rejected(tmp_path):
    result = _run_runner_cmd(["setup", "demo", "--", "--upload-pack=touch pwned"], tmp_path)
    assert result.returncode != 0
    assert "invalid git_url" in result.stderr
    assert not (tmp_path / "targets").exists()


def test_rev_starting_with_a_dash_is_rejected(tmp_path):
    result = _run_runner_cmd(
        ["setup", "demo", "https://github.com/example/demo", "--rev=--orphan"], tmp_path
    )
    assert result.returncode != 0
    assert "invalid rev" in result.stderr
    assert not (tmp_path / "targets").exists()


def test_only_https_links_become_clickable():
    mod = _load_runner()
    row = {"sha": "a" * 40, "status": "NOT A BUG", "line": "x", "reason": "r"}
    bad = mod._build_row_html({**row, "link": "javascript:alert(1)"}, "")
    assert "javascript:" not in bad
    good = mod._build_row_html({**row, "link": "https://github.com/o/r/issues/1"}, "")
    assert 'href="https://github.com/o/r/issues/1"' in good
