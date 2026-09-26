# BOB.md — How Bob built Antibody

## Files edited by Bob

The table below was derived by running:

```
grep -rnE '^> .*\*\*(write_file|apply_diff|search_and_replace|insert_content)\*\* — ' bob_sessions/ \
  | sed -E 's#^bob_sessions/([0-9]+b?)-[^/]*/bob-task-([0-9a-f]{8})[^:]*:[0-9]+:> \S+ #\1 \2 #' \
  | sort -u
```

Some lines show the start of the file's content instead of its path (`<<<<<<< SEARCH`, `"""`, `#!/usr/bin/env python3`, `---`, `customModes:`, a `# Heading`).
In tasks 01 and 02 these are edits to files the same task names elsewhere in its output.
For task 04, paths were retrieved with:

```
grep -h '\[x\] Create' bob_sessions/04-mode-skill/*.md | sort -u
```

The folder `bob_sessions/03-ledger-gate/` holds two tasks: **03** (task id `a5d4f98e`) and **03b** (task id `0baf8d72`).
**05b** is a second, shorter audit task (task id `c0c55b61`), and **06** wrote this file and README.md (task id `e8127dd7`).

| File | Task(s) |
|------|---------|
| `.antibody/sqlparse/bug_shas.txt` | 05 |
| `.antibody/sqlparse/curated.json` | 05 |
| `.antibody/sqlparse/explanations.json` | 05, 05b |
| `.antibody/sqlparse-ref/setup.json` | 01 |
| `BOB.md` | 06 |
| `.bob/custom_modes.yaml` | 04 |
| `.bob/rules-antibody/01-invariants.md` | 04 |
| `.bob/rules-antibody/02-banned-claims.md` | 04 |
| `.bob/skills/recurrence-audit/curate-guide.md` | 04 |
| `.bob/skills/recurrence-audit/SKILL.md` | 04 |
| `.bob/skills/recurrence-audit/scripts/antibody.py` | 01, 02, 03, 03b |
| `.bob/skills/recurrence-audit/scripts/antibody_plugin.py` | 01, 02, 03 |
| `.bob/skills/recurrence-audit/subagent-brief.md` | 04 |
| `.bob/skills/recurrence-audit/test-writing-guide.md` | 04 |
| `.github/workflows/antibody.yml` | 03 |
| `.gitignore` | 01 |
| `README.md` | 06 |
| `tests/test_gate.py` | 03 |
| `tests/test_probe_prove.py` | 02 |
| `tests/test_runner.py` | 01 |

## Written by subagents

Task 05 started one subagent per exposed fix; task 05b started two more.
Bob 2.2 keeps no subagent conversation, so no export shows their edits directly.
Their work is backed by the spawn briefs in the task 05 and 05b exports, each proof file's `"test"` field in `.antibody/sqlparse/proofs/`, and the screenshots in `bob_sessions/05-audit/` and `bob_sessions/05b-reattempt/`.

Check scripts in `.antibody/sqlparse/checks/` (those for `a51df6d9e2` and `ef2012a5ee` were rewritten in task 05b):

- `.antibody/sqlparse/checks/694747fdbe.py`
- `.antibody/sqlparse/checks/8c24779e02.py`
- `.antibody/sqlparse/checks/a51df6d9e2.py`
- `.antibody/sqlparse/checks/d76e8a4425.py`
- `.antibody/sqlparse/checks/e48000a5d7.py`
- `.antibody/sqlparse/checks/e58781dd63.py`
- `.antibody/sqlparse/checks/ef2012a5ee.py`

Proven tests, which the runner copied into the repository:

- `audits/sqlparse/test_694747fdbe.py` (task 05)
- `audits/sqlparse/test_d76e8a4425.py` (task 05)
- `audits/sqlparse/test_e48000a5d7.py` (task 05)
- `audits/sqlparse/test_a51df6d9e2.py` (task 05b)
- `audits/sqlparse/test_ef2012a5ee.py` (task 05b)

In task 05 the subagent for `e58781dd63` wrote a test that still passed with the bug back, so it was not proven.
The subagents for three fixes (`a51df6d9e2`, `ef2012a5ee`, `8c24779e02`) were cancelled mid-work when a message was sent to the task.
Task 05b re-attempted `a51df6d9e2` and `ef2012a5ee` — their first checks printed a measured time, which changes every run, so the probe rejected them — and proved both.

## Edited by hand

These changes to Bob's files were made by hand, not by Bob.

**After task 03:** the generic person-trailer pattern `_PERSON_TRAILER_RE` in `antibody.py`, and example.com addresses in the test strings.

**After task 03b** (Bob stopped at its cost limit after one edit, the inline-trailer cut in `_clean_subject`), in `antibody.py`: ledger reads the `NOT-A-BUG` verdict and renders after `run --shas`; `prove` accepts the test path from the repository root, records the test in its proof file, and runs the full suite in step 4; restoring the tree keeps `tests/antibody/`; ledger turns a re-proved new test into `NOW CAUGHT`, publishes only those tests and prints its summary line; page copy buttons, real command paths, one-line shas; `Cc` in the inline-trailer cut no longer matches inside a word. New file `tests/test_ledger.py`.

**After task 05:** check paths stored and shown relative to the repository (`_rel_to_root`), with a test; the sqlparse ledger republished with it (same numbers; only that path and `ledger_time` changed).

**After task 05** (commit `a094b2b`): three lines in `.antibody/sqlparse/explanations.json` corrected: `8c24779e02` no longer ends "still exposed" (its status is `NO CHANGE FOUND`); `e58781dd63` no longer says the reverse patch can't be applied (it applies; `INDICATOR` is in a second keyword table, so nothing changes); `ef2012a5ee` no longer says timing is too noisy for a test (the check printed a changing number). The ledger was republished with them (same numbers).

**After task 05b:** `antibody-accepted.yaml` written by hand (one entry, `e58781dd63`, with the reason); and in `antibody.py`'s page style, links use the accent colour and, under 480 px, cells pad less and badges wrap, so the table fits a phone; the sqlparse page was re-rendered from the same `ledger.json`.

**After task 06:** in this file, the two commands above restored to the ones actually run (their escaping was lost), the task 06 rows and sentence added, and "the subagents for three fixes" instead of "three fixes"; in README.md, the opening sentence (it claimed "most projects never check", which nothing here measures).
