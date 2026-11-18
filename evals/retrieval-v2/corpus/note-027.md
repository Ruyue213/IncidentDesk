# SCHEDULER_THROTTLED_TIME: required evidence and bounded verification

## Required combination

CPU-budget throttling is supported when the process exhausts its configured execution quota and throttled time rises despite modest machine-wide CPU utilization.

## Verification boundary

Compare the process quota, runnable delay and throttling counter deltas over the same interval; a quiet host does not prove that this process has spare execution budget.
