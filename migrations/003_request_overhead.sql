-- T6.2: time the proxy held a request before forwarding it (the router's added latency).
ALTER TABLE requests ADD COLUMN overhead_ms REAL;
