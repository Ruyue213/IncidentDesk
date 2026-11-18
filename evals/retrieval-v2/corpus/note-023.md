# LOOPBACK_PROXY_BYPASS: required evidence and bounded verification

## Required combination

Proxy misrouting is supported when a loopback request appears in the configured proxy trace, a direct request succeeds, and the bypass rule omits the exact local host or address.

## Verification boundary

Compare the application environment and bypass matching for the registered origin; do not expand a read-only diagnostic into arbitrary external proxy requests.
