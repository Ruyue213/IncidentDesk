# Admission queue saturation

An admission queue holds work before a worker begins execution. Observe queue_depth, queue_capacity, rejected_total and arrival rate together with active workers. If submissions wait while every worker is occupied, the bottleneck is in execution capacity or slow work. A healthy database connection pool does not exclude this queue bottleneck.

## Distinguishing a queue from a connection pool

Queue wait precedes task execution; connection acquisition wait happens inside a running task. Record both durations separately. A bounded queue rejects excess work; an unbounded queue may instead accumulate latency and memory. Raising capacity alone can delay rejection without improving throughput. Recommend a change only after comparing service time, arrival rate and worker utilization.
