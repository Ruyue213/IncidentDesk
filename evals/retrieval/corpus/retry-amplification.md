# Retry amplification

Retries can multiply an initial failure into overload. Compare incoming user requests with downstream attempt counts: request_attempts_total divided by logical_requests_total exposes amplification. Retry counters increasing while business traffic is flat support extra internal work. A retry storm often presents as rising queue depth and timeouts after a smaller dependency incident.

## Discriminating evidence

Inspect max_attempts, backoff, jitter and the shared request deadline. A request retried at several layers can cause multiplicative attempts. Contrast this with genuine user traffic growth. Any retry policy change requires approval and a bounded verification window; do not remove retries solely because a queue is full.
