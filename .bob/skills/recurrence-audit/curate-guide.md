# Curate Guide

## What counts as a behavioural bug

A behavioural bug is a defect that a user of the public API could observe as wrong output, a
crash, a silent data loss, or a violation of a documented contract. The user does not need to be
doing anything exotic; the wrong behaviour just needs to be reachable through the library's
interface.

Ask: "If someone shipped this version and a downstream user called this code path, would they get
the wrong answer, an exception they did not expect, or silently corrupted data?" If yes, it is
a behavioural bug.

### BUG -- two real examples from tomlkit

**Example 1: array slice assignment corrupts state silently**
Commit `4b38becd` (fix: reject array slice assignment before mutation, #600).
Thread summary: "`Array.__setitem__` rejects slice assignment only after modifying the underlying
list. For example, assigning `[9, 10]` to `a[1:2]` on `[1, 2, 3]` raises `ValueError`, but
leaves the in-memory value as `[1, 9, 10, 3]` while serialization still produces `[1, 2, 3]`."
A user calling `doc["a"][1:2] = [9, 10]` would catch an exception AND find the document
silently modified. That is observable wrong behaviour through the public API. Verdict: BUG.

**Example 2: multiline string loses its leading newline on round-trip**
Commit `8cd44f58` (fix: preserve leading newline of multiline string built with string(), #551).
Thread summary: "`tomlkit.string(value, multiline=True)` produces output that silently loses a
leading newline on round-trip ... `str(tomlkit.parse(f'k = {s.as_string()}\n')['k'])` returns
`'foo'` when it should return `'\nfoo'`." A user building a multiline TOML value with a leading
newline would get back a different string after a parse-serialize cycle. That is observable data
loss through the public API. Verdict: BUG.

---

## What does NOT count as a behavioural bug

The following changes do not affect what any user sees at runtime and must be labelled NOT-A-BUG.
Write a reason quoting the thread so the decision is traceable.

- **Typing / formatting / lint** -- no runtime effect.
- **Documentation changes** -- README, CHANGELOG, docstrings, comments.
- **Error-message wording** -- the exception type and the branch that raises it are unchanged;
  only the human-readable string is different.
- **CI / tooling configuration** -- GitHub Actions, pre-commit, tox, mypy, ruff configs.
- **Test-only changes** -- adding or fixing tests without touching source.
- **Release bookkeeping** -- version bumps, changelog entries, release commits.

### NOT-A-BUG -- two real examples from tomlkit

**Example 1: typo in an internal function name**
Commit `0b95c6e1` (refactor: Fix typo in function name, #203).
Thread summary: "I was going through the codebase while researching a different issue, and came
across this typo. Figured I'd open a small PR to fix it! Changes: Change function name
`ends_with_withespace` to `ends_with_whitespace`." The diff renames a private helper and updates
all its call sites. No user-observable output changes; the function was never part of the public
API. Verdict: NOT-A-BUG.

**Example 2: release commit bumping the version string**
Commit `d8ed1e3c` (chore: release 0.15.1, #564).
Thread summary: "Signed-off-by: Frost Ming". The diff changes `__version__ = "0.15.0"` to
`__version__ = "0.15.1"`. No logic is altered. A user who checks `tomlkit.__version__` gets a
different string, but that is not a bug fix; it is release metadata. Verdict: NOT-A-BUG.

---

## Output format for each candidate

Return exactly these three fields per SHA:

- **verdict**: `BUG` or `NOT-A-BUG`
- **reason**: one sentence quoting a key phrase from the thread that justifies the verdict
- **link**: the URL from the thread (the PR or issue link)

When the thread is too thin to judge (no description, no reproduction, no rationale), say
`NOT-A-BUG` and write "thread gives no evidence of user-observable misbehaviour" as the reason.
