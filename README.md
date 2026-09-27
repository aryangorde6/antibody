# Antibody

Every project fixes bugs, but a fix only stays fixed if some test notices when it goes away.
Antibody puts a project's past bug fixes back, one at a time, on today's code and runs the project's own tests; if no test notices a fix being taken out, that bug can come back unnoticed.

## Result on sqlparse

**Before: 13 of 20 caught. After: 18 of 20 caught.**

Numbers come from [`audits/sqlparse/ledger.json`](audits/sqlparse/ledger.json).
Live ledger page: <https://antibody-ledger.vercel.app/sqlparse/>

## How to run

Requires Linux (tested on Ubuntu), git, Python 3.12, and [uv](https://github.com/astral-sh/uv).
The runner imports Unix-only modules (`fcntl`, `resource`), so on Windows, run it inside WSL.

**With IBM Bob:** switch Bob to the Antibody mode and ask it to audit a repository (the recurrence-audit skill handles the full workflow).

**By hand:** run each step from the repository root:

```
python3 .bob/skills/recurrence-audit/scripts/antibody.py <step>
```

Steps, in order: `setup`, `candidates`, `run`, `probe`, `prove`, `ledger`, `gate`.

**CI gate:** `.github/workflows/antibody.yml` fails the build while a fix is still exposed, unless `antibody-accepted.yaml` lists it with a one-sentence reason for the acceptance.

## What it can't tell you

- Rows with no data are unknown, never safe.
- "Caught" means a test noticed the regression, not that the test is perfect or complete.
- Antibody works with Python and pytest projects only.

## Before the event

Before the hackathon started we ran our own script — no Bob — against 18 Python libraries and published the results at <https://github.com/aryangorde6/antibody-census>.
The headline: **479 bug fixes undone in 18 Python libraries, and 139 times every test still passed.**

## Built with IBM Bob

Bob handles the parts that need judgment:

- Reading the bug report and pull-request thread for each candidate fix, and deciding whether it was a real behavioural bug.
- Starting one subagent per exposed fix to write and prove a regression test.
- Writing the plain-English explanation for each row in the ledger.

The runner handles the parts that need determinism:

- Reversing each fix and running the project's own test suite.
- Re-checking every proof file to confirm the test still catches the bug.

Bob session exports are in [`bob_sessions/`](bob_sessions/).
See [`BOB.md`](BOB.md) for a detailed map of every file Bob created or edited.

## Data sources

| Source | Licence / note |
|--------|----------------|
| Each audited project's GitHub repository (git history, and the issue and pull-request pages its fixes link to) | Project's own licence |
| [GitHub Advisory Database](https://github.com/advisories) | CC-BY 4.0 |
| [PyPI](https://pypi.org/) | Used to install test dependencies |

No personal data is kept: names, handles, and email addresses are dropped when threads are saved, and thread text stays on the machine that ran the audit, out of the repository.

## Licence

MIT — see [LICENSE](LICENSE).
