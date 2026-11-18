# STREAM_PENDING_BYTES: required evidence and bounded verification

## Required combination

Slow-consumer backpressure is supported when per-client pending bytes grow, consumer acknowledgements stall, and producer work stays stable while memory follows buffered output.

## Verification boundary

Compare queue limits and acknowledgement progress for the affected stream before dropping data or restarting producers; global throughput alone hides a single slow subscriber.
