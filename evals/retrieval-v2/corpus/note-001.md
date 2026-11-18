# POOL_ADMISSION_WAIT: required evidence and bounded verification

## Required combination

A smaller pool limit is a supported explanation only when active connections reach that limit, acquisition wait rises, and query duration remains near its earlier baseline.

## Verification boundary

Compare effective limits per instance and connection hold time before considering a supervised capacity change; a release timestamp alone does not establish causation.
