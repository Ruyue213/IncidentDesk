# DEPENDENCY_DEADLINE: required evidence and bounded verification

## Required combination

An upstream timeout is supported when the same dependency path has rising client latency and deadline errors while local queue and database timings stay stable.

## Verification boundary

Separate connection establishment from response wait, compare the downstream path across instances, and inspect retry amplification before assigning ownership.
