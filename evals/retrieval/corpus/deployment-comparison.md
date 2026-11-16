# Comparing a change with incident evidence

A recent deployment is a candidate cause, not a confirmed root cause. Compare an immutable configuration revision and release identity with the incident onset. Record the before-and-after value, affected process and observation timestamps. A working-tree diff only describes local edits and does not prove that those edits are active in a running binary.

## Controlled verification

Use comparable observation windows with similar traffic to test a candidate explanation. Check contradictory evidence and alternate causes before proposing rollback. A rollback is an approved action with an explicit target revision, bounded verification and a recovery plan. Merely seeing a recent commit is insufficient evidence for executing it.
