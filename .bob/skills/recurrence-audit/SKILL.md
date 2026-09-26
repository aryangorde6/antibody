---
name: recurrence-audit
description: >-
  Use when the user wants to audit a project for unprotected bug fixes, find
  which past bugs could recur, and write regression tests that close the gaps.
---

# Recurrence Audit

Every runner call is exactly:

  python3 .bob/skills/recurrence-audit/scripts/antibody.py <command> ...

from the repository root, never after `cd` or chained with `&&`, so one approval rule covers them
all. `setup`, `candidates`, `run`, and `ledger` take minutes; run them in the background and read
their output file until they finish (a foreground command stops after 270 seconds).

## Step 1 -- Setup and candidate collection

If `.antibody/<name>/setup.json` does not exist yet, run:

  python3 .bob/skills/recurrence-audit/scripts/antibody.py setup <name> <git_url>

Ask the user for the repository URL if it was not given. Then run:

  python3 .bob/skills/recurrence-audit/scripts/antibody.py candidates <name>

Read the summary it prints. Do not edit its output.

## Step 2 -- Curate

Split the candidates from `.antibody/<name>/candidates.json` into batches of about 6.
Start one **explore** subagent per batch, all at once.

Explore subagents cannot load skills. Give each subagent:

- the path of `.bob/skills/recurrence-audit/curate-guide.md` to read first
- the paths of its candidates' source files
- the paths `.antibody/<name>/diffs/<sha>.patch` and `.antibody/<name>/threads/<sha>.md` for each
  candidate (thread files are gitignored, so search tools skip them; read them by path)

Each subagent returns, for every SHA in its batch: `BUG` or `NOT-A-BUG`, one sentence of reason
quoting the thread, and the thread link.

Merge the answers into `.antibody/<name>/curated.json`: a JSON object mapping each full SHA to
`{"verdict": "BUG" or "NOT-A-BUG", "reason": "...", "link": "..."}`.
NOT-A-BUG rows stay in the file with their reason.

## Step 3 -- Run

Write the BUG SHAs (one full SHA per line) to a temporary file, then run:

  python3 .bob/skills/recurrence-audit/scripts/antibody.py run <name> --shas <file>

## Step 4 -- Close the gaps

For every ESCAPED row, start one **general** subagent, all at once.
Never give several rows to the same subagent.

Give each subagent the text of `.bob/skills/recurrence-audit/subagent-brief.md` with the
row's fields filled in (sha, subject, link, source_files, sha10, name).

## Step 5 -- Collect results

A row's status comes only from its files in `proofs/` and `probes/`, not from the subagent's
returned JSON. Read the proof file for each row before recording its status.

## Step 6 -- Write explanations

Write `.antibody/<name>/explanations.json`: a JSON object mapping each full SHA to one plain
English sentence -- what the bug was and what protects it now. `ledger` reads this file.

## Step 7 -- Publish ledger

Run:

  python3 .bob/skills/recurrence-audit/scripts/antibody.py ledger <name> --publish

Report its summary line exactly as printed, with no paraphrase.
