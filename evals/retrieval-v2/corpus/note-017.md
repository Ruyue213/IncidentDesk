# CACHE_SCHEMA_EPOCH: required evidence and bounded verification

## Required combination

A cache rollout mismatch is supported when one application epoch reads entries written under another schema namespace, misses are low, and decoding failures track mixed versions.

## Verification boundary

Compare reader and writer namespace epochs and inspect a redacted entry shape before any scoped invalidation; a cache hit is not proof that the value matches the reader schema.
