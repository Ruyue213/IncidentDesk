# DELIVERY_LEASE_EXPIRED: required evidence and bounded verification

## Required combination

Expired delivery leases are supported when processing exceeds the configured visibility interval, old unacknowledged deliveries return, and durable completion records already exist.

## Verification boundary

Compare completion timestamps with lease renewals and acknowledge only after durable completion; inspect the receipt before repeating an uncertain business effect.
