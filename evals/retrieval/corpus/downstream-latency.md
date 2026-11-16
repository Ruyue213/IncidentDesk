# Slow external dependency

A remote dependency can dominate request duration even when local workers and CPU appear healthy. Compare dependency_request_seconds, dependency error rate and client spans for the same time window. Connection setup, DNS lookup, response wait and local connection acquisition are different phases. A local connection-acquisition timeout alone does not prove that the remote server is slow.

## Evidence and safe follow-up

If remote response time rises before local queue depth, dependency slowness may be upstream of the queue buildup. Correlate dependency span duration with the affected operations and request rate. Compare a read-only probe with production telemetry. Do not claim causation from two unrelated time windows or propose increasing all timeouts without measuring the complete latency budget.
