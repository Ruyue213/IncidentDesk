# TRANSACTION_LOCK_WAIT: required evidence and bounded verification

## Required combination

Lock contention is supported by waiting transactions that identify the same blocking owner, a long open transaction, and low query execution CPU outside the wait.

## Verification boundary

Trace the owner transaction and its lifetime before proposing a targeted intervention; distinguish lock wait from connection admission wait and retain blocked statement identities.
