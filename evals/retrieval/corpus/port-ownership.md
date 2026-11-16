# Port ownership and failed startup

An EADDRINUSE or address already in use error indicates that a bind operation failed because an address is occupied. Identify the listening PID, executable and working directory for the exact host and port. An HTTP 200 response from that port may belong to an older process or an unrelated application and does not prove that the intended binary started.

## Safe process verification

Compare the listener with the recorded managed process identity and expected executable before proposing a restart. Never kill an arbitrary port owner. A stale PID file is insufficient evidence because operating systems reuse process IDs. A startup verification should include port ownership, health response and a read-only business query against the same endpoint.
