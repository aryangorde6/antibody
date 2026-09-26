# Invariants

These rules hold for every run. Violating any of them produces a wrong result.

1. **Zero tests collected is never a pass.**
   If a test file is found but no tests are collected, the run is a configuration error, not a green result. Treat it as inconclusive.

2. **A red baseline means no verdict.**
   If the test suite already fails before the fix is put back, the test is not an independent witness. Do not record PROVEN until the baseline is green.

3. **Statuses come only from runner output.**
   PROVEN, GAVE_UP, ESCAPED, STILL_EXPOSED, and similar labels must be drawn from what `probe` and `prove` print. A subagent's JSON return, a code-level reading, or a reasoning step is not a verdict.

4. **The tree is restored after every put-back.**
   `prove` and `probe` restore the source tree automatically on every run. If a run is interrupted (timeout, crash), run the same command again before doing anything else. Never leave the tree in a modified state between steps.
