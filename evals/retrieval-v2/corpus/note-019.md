# REPLICA_APPLY_POSITION: required evidence and bounded verification

## Required combination

Replica visibility lag is supported when a primary commit is durable, the selected read replica has not reached its apply position, and direct primary reads already see the write.

## Verification boundary

Record the commit and apply positions and the actual read route before changing consistency settings; successful transport does not prove that a replica has applied a transaction.
