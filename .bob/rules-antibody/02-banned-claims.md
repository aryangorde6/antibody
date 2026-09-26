# Banned Claims

Never use the words below unless a proof file explicitly says so.

- **safe** -- a fix is not safe just because it has tests; it is safe only when `prove` records PROVEN.
- **immune** -- no commit is immune to regression; the question is always whether it is currently tested.
- **protected** -- do not call a row protected unless `.antibody/<name>/proofs/<sha10>.json` exists and contains status PROVEN.
- **caught** -- do not call a row caught unless the same proof file says so.

**No status without a proof file.**
Rows that have not been through `prove` have no status. Leave the ledger field blank or say UNKNOWN. Do not infer a status from the diff, the commit message, or a test that exists in the suite but was not run by `prove`.
