# ACCOUNT_RATE_LIMIT: required evidence and bounded verification

## Required combination

Provider-side rate limiting is supported by dependency responses carrying 429 and Retry-After, normal local saturation indicators, and a changed shared-account allowance.

## Verification boundary

Inspect the account-wide consumers and their retry delays; the application returning a 429 does not by itself show which side imposed the limit.
