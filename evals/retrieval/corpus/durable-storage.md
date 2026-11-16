# Durable append and disk pressure

A service that appends a write-ahead log can stall when fsync_seconds rises, disk free space falls or the filesystem returns ENOSPC. A successful process health check does not prove that durable writes can complete. Compare write latency and persistence errors with disk utilization and available capacity in the same window.

## Preserve existing data

Distinguish disk capacity exhaustion from a slow synchronization device. A missing snapshot does not justify deleting a write-ahead log. Collect file sizes and free space without altering durable files. Compaction, deletion, snapshot replacement and replay changes are stateful operations that require a reviewed recovery plan and explicit approval.
