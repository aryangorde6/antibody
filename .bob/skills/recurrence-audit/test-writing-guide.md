# Test-Writing Guide

## Core rules

**Test behaviour through the public API.**
Import only from the library's public namespace. Do not reach into private modules, internal
helpers, or implementation details. The test must fail when the bug is present and pass when
it is fixed using only the same entry points a downstream user would call.

**Deterministic.**
Every run must produce the same outcome. No sleeps, no network calls, no randomness, no
`time.time()`, no `datetime.now()`, no `random`, no `tempfile` with externally visible names.
If the test needs a file, write it to a `tempfile.TemporaryDirectory` scoped to the test.

**No raw timing.**
Never assert that something completes "within N seconds". A busy CI machine will flap.

## Performance tests (detecting algorithmic regressions)

Use the **n versus 4n growth ratio**, not a raw time.

- Pick a small `n` such that the slow (bug-present) side finishes in about one second, because
  `prove` runs the test six times.
- Time each size as the **minimum** of `timeit.repeat(number=1, repeat=3)`. The minimum filters
  out OS noise; using the mean or a single run causes intermittent failures on a loaded machine.
- Assert that `ratio = time_at_4n / time_at_n < 8`. Linear growth gives a ratio near 4; quadratic
  gives a ratio near 16. The threshold of 8 leaves room for constant-factor variation while still
  catching quadratic regression. Do not use a tighter threshold.

```python
import timeit

def time_op(size):
    # return the minimum of 3 single runs
    return min(timeit.repeat(lambda: my_op(size), number=1, repeat=3))

t_n  = time_op(n)
t_4n = time_op(4 * n)
assert t_4n / t_n < 8, f"growth ratio {t_4n / t_n:.1f} suggests quadratic behaviour"
```

## What `prove` accepts as a failure

`prove` puts the bug back (reverts the fix), runs the test, then restores the fix and runs again.

- A test that **raises `AssertionError`** (or any exception) with the bug present and **passes
  cleanly** with the fix present is PROVEN.
- A test that passes in both states is not a witness; `prove` will report STILL_EXPOSED.
- A test that fails in both states is not a witness; `prove` will report a red baseline and stop.
- Zero tests collected is never a pass (see invariants).

So: write a test that asserts the correct behaviour. If the bug is present, the assertion fails.
If the fix is present, the assertion passes. That is the only shape `prove` can verify.

## Style

- One test function per file. Name it `test_<description>`.
- No test classes, no fixtures, no conftest. A single plain function is enough.
- Import only the standard library and the library under audit.
- Keep the test short: reproduce the issue, assert the correct result, done.
