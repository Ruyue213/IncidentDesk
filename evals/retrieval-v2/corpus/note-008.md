# WAL_FSYNC_BACKLOG: similar symptom, different explanation

## Counterexample

WAL_FSYNC_BACKLOG may be reported after a large intentional import. A higher write arrival rate with normal device latency supports demand growth rather than device regression.

## Interpretation boundary

This counterexample must not substitute for the required evidence in the paired investigation note. Preserve the observed contradiction rather than declaring recovery.
