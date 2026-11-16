# Optional cache versus cache outage

An optional cache can be intentionally disabled by configuration. cache_enabled=false means requests use the documented uncached path; a zero cache hit count is then expected. Distinguish that mode from cache_enabled=true with connection errors, elevated lookup latency or an unexpectedly falling hit ratio.

## Diagnose the configured mode

Read effective configuration and startup arguments before treating a missing cache process as an incident. If the uncached mode is supported, validate business correctness and the resulting load budget. Enabling a cache introduces a dependency and changes behavior; do not start an extra service merely to make a dashboard indicator green.
