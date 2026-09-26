# Subagent Brief

You are closing a regression gap for one commit. Follow these steps exactly.

## Your row

- sha: {{sha}}
- subject: {{subject}}
- thread link: {{link}}
- source files changed: {{source_files}}
- diff: `.antibody/{{name}}/diffs/{{sha}}.patch`
- thread: `.antibody/{{name}}/threads/{{sha}}.md`

## Setup

Read `.bob/skills/recurrence-audit/test-writing-guide.md` first.
Then read the diff and thread for this row. Files under `targets/` and thread files are
gitignored, so grep and glob skip them; read them by path, or run `grep -rn` as a shell command.

Every runner call is:
  python3 .bob/skills/recurrence-audit/scripts/antibody.py <command> ...
from the repository root, never after cd or chained with &&.
`probe` and `prove` may wait for another subagent's run to finish; that is expected.
Call them with a 270-second timeout. If a call is stopped by the timeout, run it again.

## Step 1: Write a check and run probe

Write a check at `.antibody/{{name}}/checks/{{sha10}}.py`.
A check is a plain Python script. It must:
- import and call the library through its public API
- print one line: `DIFFERENCE FOUND: <value_with_bug> vs <value_without_bug>` or `NO CHANGE FOUND`

Run:
  python3 .bob/skills/recurrence-audit/scripts/antibody.py probe {{name}} {{sha}} .antibody/{{name}}/checks/{{sha10}}.py

- On NO CHANGE FOUND: try one more input drawn from the thread. If that also finds nothing,
  stop and return status NO_CHANGE.
- On INVALID or INCONCLUSIVE: try at most two other inputs. If none works, stop and
  return status UNREACHED with the inputs you tried. The row stays STILL EXPOSED.

## Step 2: Write a test and run prove

Write a single test function at `targets/{{name}}/tests/antibody/test_{{sha10}}.py`.
Follow the test-writing guide exactly.

Run:
  python3 .bob/skills/recurrence-audit/scripts/antibody.py prove {{name}} {{sha}} targets/{{name}}/tests/antibody/test_{{sha10}}.py

Read the output each time. Make at most 4 attempts total. On each failure, adjust the test
based on what the output says.

## Budget

You have about 25 turns. Plan:
- 1 turn: read guide + diff + thread
- up to 3 turns: check + probe (one check, at most 2 retries)
- up to 4 turns: write test + prove (at most 4 attempts)
- 1 turn: return answer

Touch nothing else.

## Return format

Return JSON only (no prose before or after):

```json
{
  "sha": "{{sha}}",
  "status": "PROVEN | GAVE_UP | NO_CHANGE | UNREACHED",
  "check_file": ".antibody/{{name}}/checks/{{sha10}}.py",
  "test_file": "targets/{{name}}/tests/antibody/test_{{sha10}}.py",
  "attempts": 0,
  "what_it_checks": "one sentence describing what the test asserts",
  "reason_if_gave_up": "leave empty if status is PROVEN"
}
```

The ledger trusts the proof and probe files, not this JSON. Return it anyway so the
orchestrating agent can reconcile.
