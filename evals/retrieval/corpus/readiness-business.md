# Process health and business correctness

An HTTP 200 from /health may only mean that the process event loop is alive. Readiness and business correctness require separate checks. A graph query can return a wrong answer, an empty shape or an internal error while the health endpoint succeeds. Check a fixed read-only business operation with expected JSON values as well as expected status.

## Probe contract

Record target identity, expected response, timeout and latency threshold before running a probe. Probe failure is evidence of an unmet assertion, not proof of a particular root cause. Avoid arbitrary writes as a health test. Verify the same managed instance and endpoint rather than a different process on a different port.
