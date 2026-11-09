# Remote machine HTTP retries

Remote `machine.start(...)` and `machine.send(...)` automatically retry HTTP
429, 500, 502, 503 and 504 and connection failures/timeouts before a response.
Other 4xx responses are final. A machine start retains its canonical instance
key; send retains the caller's `idempotency_key`, payload and event on every
attempt. These HTTP retries are separate from state-work/step retries.

```python
# Existing machine and configured DELTA_API_URL/TOKEN/ORG_ID/PROJECT_ID:
run_id = await approval.start({"id": 42}, retry_budget_s=15)
receipt = await approval.send(
    (42,), "approved", idempotency_key="delivery-42", retry_budget_s=15,
)
```

The default total budget is **60 seconds per request**. Override it using
`DELTA_HTTP_RETRY_BUDGET_S` or `retry_budget_s=`; the explicit argument wins.
Zero disables retries. Values must be finite and non-negative. The reusable
transport also accepts `MachineRemoteClient(retry_budget_s=15)` from
`avalanche.machine_remote`; close it with `async with` on its owning event loop.
The machine convenience methods create and close that transport automatically.

`Retry-After` is honored as seconds or an HTTP date. Otherwise waits have full
jitter and exponential growth from 0.5 seconds, capped at 10 seconds. A wait
longer than the remaining budget consumes only the remainder and returns the
last real error, without sending an early request. `MachineRemoteError` retains
HTTP status, a safe reason code and `Retry-After`, never arbitrary provider bodies.
Connection failures retain the actual exception as their cause and identify its
type; manually retry an uncertain result with the same key and payload.

Retries log at INFO through the standard `avalanche.http_retry` logger. Enable
that logger in your application's logging configuration. Notices contain only
delay, status/reason or exception type, and attempt number: no bearer tokens,
request/response bodies or URLs. Network I/O and waits are native async; task
cancellation propagates immediately, with no retry thread.

For lower-level transport use, GET/HEAD and keyed mutations can be repeated;
unkeyed mutations retry only 429. No successful response body is automatically
replayed. The remote machine API currently uses JSON responses, not SSE or
signed uploads.
