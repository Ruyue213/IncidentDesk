# WAL_FSYNC_BACKLOG: required evidence and bounded verification

## Required combination

Durability pressure is supported when WAL flush duration and disk queue depth rise together while transaction work and incoming write rate remain stable.

## Verification boundary

Compare free space, device latency and flush completion timestamps; do not delete the active journal to improve a dashboard counter.
