# Task 07 review: what we did with each finding

Task 07 (`28143b43`) was a read-only review by Bob in Ask mode, run on Samruddhi's account against commit `d75f396`.
Each finding was checked with one command before anything changed. Only finding 1 held up.

| # | Finding | Verdict | Check |
|---|---------|---------|-------|
| 1 | `antibody.py` imports `fcntl` and `resource`, which don't exist on Windows | **Confirmed.** Fixed in README.md: the requirements line now says Linux, and WSL on Windows. The code is unchanged. | `grep -nE '^import (fcntl\|resource)' .bob/skills/recurrence-audit/scripts/antibody.py` |
| 2 | CI calls `python3.12` without `setup-python`, so the gate could fail or pass silently | Not confirmed. `python3.12` is on GitHub's `ubuntu-latest` image: the gate passed there for `d75f396` ([run 36269943717](https://github.com/aryangorde6/antibody/actions/runs/36269943717)). The step runs with `set -e`, so a missing `python3.12` stops the job; it can't pass silently. | `bash -c 'set -e; x="$(no_such_cmd)"; echo passed'` never prints `passed`; it exits 127 |
| 3 | README runs `python3`, but the runner needs 3.12 | Not confirmed. Python 3.12 is what the audited project runs in: the runner creates that environment with `uv venv --python 3.12`. The runner itself parses under 3.10 and 3.11, and runs under 3.14 on the machine that made the audit. | `python3 -c "import ast; ast.parse(open('.bob/skills/recurrence-audit/scripts/antibody.py').read(), feature_version=(3, 10))"` |
| 4 | `.antibody/<name>/ledger.json` is git-ignored, so CI skips the head check | Not confirmed. The file is committed; `.gitignore` leaves out only `.antibody/*/threads/`. | `git ls-files .antibody/sqlparse/ledger.json` |
| 5 | `ledger.json` timestamps are dated 2026-09-26, "in the future" | Not confirmed. The audit ran on 26 Sep 2026, during the event. | `git log -1 --format=%cd -- audits/sqlparse/ledger.json` |
| 6 | STILL EXPOSED and NO CHANGE FOUND rows count toward the 20 | No change (Bob also said none was needed). The page defines the 20 next to the number: every past bug whose fix was taken out and whose tests ran. The rows no test noticed are the ones the number is about. | `grep -n 'summary-note' site/sqlparse/index.html` |
| 7 | `curated.sha256` in `ledger.json` has 63 characters | Not confirmed. It has 64. | `python3 -c "print(len('fa3d4b23160e245c30c65694c9414ffac50379011e0dc23d62e5fbdaead2c9e3'))"` prints 64 |

The task cost 4.69 Bobcoins (screenshot `axiom_samruddhi_task07_review_summary.png`).
