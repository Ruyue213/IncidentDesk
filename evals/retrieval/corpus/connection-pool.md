# Database connection acquisition pressure

ConnectionAcquireTimeout means a running request could not lease a database connection before its deadline. Compare pool_active against pool_limit, pool_waiters and connection_acquire_seconds. An application connection pool can saturate while database CPU and query execution latency remain normal. The request is waiting to acquire a resource, rather than executing a slow SQL query.

## Confirming a pool configuration regression

Compare the effective pool limit before and after a configuration change. A sharp limit reduction with more waiters and unchanged query time supports acquisition pressure. Check leaked connections and transaction duration as alternatives. Increasing the limit is a proposed configuration change that requires review and a database connection budget; it is not an automatic repair.
