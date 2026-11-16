# Slow clients and response backpressure

Clients that stop reading can retain response buffers and occupy connection slots even when the business computation is fast. Compare response_write_seconds, pending_output_bytes, active client connections and computed request time. Low CPU and normal database latency do not rule out network backpressure.

## Distinguishing output pressure

If computation finishes promptly but response writes stall, inspect client read rate and socket send buffers. Contrast response backpressure with an admission queue that fills before computation begins. Connection limits, write deadlines and load-shedding settings should be reviewed against client behavior rather than changed automatically.
