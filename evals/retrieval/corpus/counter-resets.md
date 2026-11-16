# Counter resets and observation windows

Monotonic counters reset when a process restarts. A lower request_total value after restart does not mean requests were undone. Compare process_start_time_seconds and metric instance identity before subtracting two snapshots. A negative raw difference is invalid as an error-rate estimate across a reset.

## Valid comparisons

Derive rates only within a continuous process lifetime or use a counter-reset-aware rate function over enough samples. Mark missing history explicitly. Current metrics describe the collection instant; a dashboard requested for yesterday cannot reconstruct historical rates from today's single snapshot. Separate unavailable evidence from measured zero errors.
