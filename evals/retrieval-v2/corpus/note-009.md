# CLOCK_OFFSET_STEP: required evidence and bounded verification

## Required combination

A clock-related expiry problem is supported by an abrupt wall-clock offset, clustered token expiry failures, and stable monotonic request durations.

## Verification boundary

Compare time synchronization events and wall-clock versus monotonic timestamps before changing token lifetimes; absence of an expiry log does not prove synchronized clocks.
