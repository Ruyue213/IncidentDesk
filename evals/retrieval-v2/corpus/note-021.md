# RESOLVER_CACHE_AGE: required evidence and bounded verification

## Required combination

A stale resolver cache is supported when the application resolves a retired endpoint, the authoritative answer has changed, and connection attempts follow the cached address until expiry.

## Verification boundary

Compare application resolver answers, their ages and the authoritative address; separate resolver caching from an already-open connection that merely keeps an older peer.
