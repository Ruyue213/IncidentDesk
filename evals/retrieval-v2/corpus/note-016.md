# TRANSACTION_LOCK_WAIT: similar symptom, different explanation

## Counterexample

TRANSACTION_LOCK_WAIT in a sampled query plan does not show a current blocker. When no owner is present but all connection slots are occupied, investigate admission capacity.

## Interpretation boundary

This counterexample must not substitute for the required evidence in the paired investigation note. Preserve the observed contradiction rather than declaring recovery.
