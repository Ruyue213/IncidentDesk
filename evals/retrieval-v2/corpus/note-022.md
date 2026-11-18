# RESOLVER_CACHE_AGE: similar symptom, different explanation

## Counterexample

RESOLVER_CACHE_AGE can be low and current even while a keep-alive socket still reaches an old peer. A correct fresh lookup then contradicts stale DNS as the sole cause.

## Interpretation boundary

This counterexample must not substitute for the required evidence in the paired investigation note. Preserve the observed contradiction rather than declaring recovery.
