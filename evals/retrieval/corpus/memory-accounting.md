# Memory measurements and units

A large application counter is not necessarily resident memory. memory_bytes may represent logical graph storage or allocated capacity, whereas process_rss_bytes reports resident pages. Compare metric help text, units, allocator accounting and operating-system RSS before declaring a leak. A bytes-to-mebibytes display error can exaggerate apparent resource use by orders of magnitude.

## Confirming a memory problem

Compare trends under equivalent load and inspect out-of-memory events, memory limits and sustained RSS growth. A single cumulative allocation counter can increase even when memory is freed. Do not infer physical memory exhaustion from a logical node count or a monotonically increasing lifetime allocation total.
