# TLS_CHAIN_REJECT: required evidence and bounded verification

## Required combination

A trust-chain failure is supported when handshake verification fails before an HTTP response and the deployed certificate chain differs from the client trust store.

## Verification boundary

Inspect certificate validity and intermediate-chain presentation separately from HTTP authorization; do not disable verification as a diagnosis shortcut.
