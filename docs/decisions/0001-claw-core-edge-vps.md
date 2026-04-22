# ADR 0001: Claw-Core VPS and Mac Edge Split

## Status

Accepted after review. Core/Edge implementation can start after this ADR
revision is merged; implementation PRs must preserve the constraints below.

## Context

Claw currently depends on a single host. When the Mac sleeps, Telegram/Web text
flows, long-running jobs, observability, and orchestration stop with it. PR#4
made jobs durable, PR#5 added observability export, PR#6 added process managers,
and PR#7 added capability manifests. Those layers make a Core/Edge split viable.

The split must preserve existing behavior while making local-only capabilities
explicitly unavailable when the Mac is offline.

## Decision

Run Claw as two roles:

- **Core** on a VPS: Telegram/Web transport, brain, memory, LLM router,
  approvals, durable jobs, pipeline, evals, dashboard, observability, and
  capability registry.
- **Edge** on the Mac: Computer Use, Chrome CDP, browser automation tied to an
  authenticated local profile, terminal bridge, and macOS-specific skills.

Core owns user-facing request termination. Edge exposes capability endpoints to
Core. Core must never import or branch on macOS implementation details; it only
sees capability manifests and health states.

## Protocol

Core and Edge communicate over HTTPS using versioned A2A messages:

- `GET /.well-known/claw-edge.json`: edge identity, protocol version,
  supported capabilities, auth key id, and health summary.
- `POST /a2a/v1/tasks`: submit an idempotent task with `job_id`, `trace_id`,
  `capability`, `action`, `payload`, `deadline_ms`, `idempotency_key`, and an
  optional `callback_url` for long-running tasks.
- `GET /a2a/v1/tasks/{task_id}`: poll status and result metadata.
- `POST /a2a/v1/tasks/{task_id}/cancel`: request cancellation.
- `GET /a2a/v1/health`: liveness, readiness, load, queue depth, and degraded
  capability reasons.

All messages include `protocol_version`, `schema_version`, `trace_id`,
`root_trace_id`, `span_id`, `job_id`, and `artifact_id` when available. Payloads
must be JSON only. Large artifacts are referenced by artifact id or signed URL,
not embedded in task payloads.

Polling remains the baseline contract. `callback_url` is an optimization for
multi-minute Computer Use tasks; callbacks must be signed, idempotent, and
advisory. Core verifies final task state with `GET /a2a/v1/tasks/{task_id}`
before marking the job step complete.

## Connectivity Layer

Core must not require public inbound ports on the Mac. The approved
connectivity options are:

- Preferred: a private mesh network such as Tailscale or WireGuard.
- Acceptable fallback: Cloudflare Tunnel or equivalent outbound-only tunnel.

Router port forwarding and dynamic DNS to the Mac are rejected for the initial
implementation. Edge advertises its private overlay or tunnel URL in
`/.well-known/claw-edge.json`; Core stores only that endpoint and capability
state. TLS and A2A HMAC still apply inside the private connectivity layer.

## Latency Budget

Core text-only flows must not wait on Edge unless a selected capability requires
Edge execution.

- Telegram/Web text response target: p95 under 8 seconds for text-only flows.
- Edge health check target: p95 under 500 ms from Core to Edge.
- Edge task admission target: p95 under 1 second.
- Interactive Computer Use step target: p95 under 5 seconds per action after
  admission.
- Core timeout for unavailable Edge: 2 seconds for health, 10 seconds for task
  admission after health is ready, then mark capability degraded.

If the latency budget is exceeded, Core records a degraded capability event and
continues with text-only behavior where possible.

## Authentication

Core and Edge use mutual authentication:

- TLS is required for all remote traffic.
- Requests are signed with HMAC using `A2A_SECRET` and include timestamp,
  nonce, key id, and canonical body hash.
- Clock skew tolerance is 5 minutes.
- Nonces are single-use per key id for at least 10 minutes.
- Edge rejects unsigned, expired, replayed, or unknown-version requests.
- Core treats auth failures as capability unavailable and emits audit events.

Approval HMAC remains separate from A2A HMAC. A2A auth proves service identity;
approval auth proves user authorization for risky actions.

## Retries And Idempotency

Core retries only idempotent submissions with the same `idempotency_key`.

- Retry schedule: 1s, 3s, 10s, then fail the Edge step.
- Retryable: connection reset, timeout before admission, HTTP 429, HTTP 503.
- Non-retryable: HTTP 400, HTTP 401, HTTP 403, unknown capability, schema
  mismatch, approval required.
- Edge must return the existing task result for duplicate idempotency keys.
- Cancellation is best-effort and also idempotent.

Core persists every attempt as a job step so "why did Claw do X" remains
answerable from audit artifacts.

## Backpressure

Edge reports `queue_depth`, `running_tasks`, `capacity`, and `retry_after_ms`.
Core must stop dispatching new Edge tasks when:

- Edge readiness is false.
- Edge returns HTTP 429.
- Edge queue depth is above the advertised capacity.
- The task deadline cannot be met.

Backpressured work moves to `waiting_edge` or `failed_degraded` depending on
user intent and job policy. Core may continue unrelated text-only work.

## Degraded Mode

The Edge health check is the source of truth for dispatch. If health fails,
times out, or reports readiness false, Core immediately enters degraded mode for
Edge capabilities and does not attempt task admission. This covers Mac sleep,
hibernation, shutdown, network partition, and tunnel failure.

When the Mac is off, asleep, or Edge is unreachable:

- Core remains online and processes Telegram/Web text messages.
- Core marks Edge capabilities as `unavailable` or `degraded` with a concrete
  reason and timestamp.
- Computer Use, Chrome CDP, local terminal, and authenticated browser actions
  return explicit unavailable messages.
- Jobs requiring Edge pause as `waiting_edge` if resumable, otherwise fail with
  a typed degraded outcome.
- Evals must pass in Core-only mode for text flows.

Degraded mode must never look like a generic exception, empty response, or
silent timeout.

## Artifact Ownership

Edge owns temporary raw binary artifacts produced by Edge-local capabilities,
including screenshots, Chrome dumps, screen recordings, and browser exports.
Edge uploads large artifacts to object storage such as S3/R2 or keeps them in
Edge-local temporary storage behind signed URLs. Core stores artifact metadata,
lineage, checksum, content type, size, retention, and signed URL only.

Core must not store raw browser profiles, cookies, screenshots, or large binary
dumps in SQLite. Signed URLs must expire, and Edge remains responsible for raw
artifact cleanup according to the advertised retention policy.

## Secret Management

Core and Edge keep separate environment files and separate service identities.

- Core owns LLM, Telegram/Web, database, approvals, and dashboard secrets.
- Edge owns local browser, Computer Use, terminal bridge, and Mac-specific
  secrets.
- Edge owns credentials needed to upload Edge artifacts to object storage.
- Mesh VPN or tunnel credentials are separate from A2A and approval secrets.
- Secrets are never sent over A2A payloads.
- Rotating `A2A_SECRET` requires dual-key support: current key and next key.
- Logs and artifacts must redact tokens, signed URLs, cookies, and browser
  profile paths.

VPS backups must include durable jobs, artifacts, memory, and observe data, but
must not include Edge-local browser profile material.

## Version Skew

A2A messages carry `protocol_version` and `schema_version`.

- Core accepts Edge versions in an explicit compatibility window.
- Edge advertises supported protocol versions in its identity document.
- Unknown major versions fail closed.
- Unknown optional fields are ignored.
- Missing required fields fail with a typed schema error.
- Core deployment must be backward-compatible with the currently running Edge
  for at least one rollout cycle.

Contract tests must cover one older compatible Edge fixture and one incompatible
Edge fixture.

## Observability

Every Core to Edge request must propagate trace context. Core records:

- capability selected
- Edge health at dispatch time
- retries and backpressure decisions
- idempotency key
- final task status
- degraded reason, if any

OpenTelemetry is for technical traces. `observe.py` and artifacts remain the
product audit path.

## Rollout Plan

1. Add contract tests and fixtures for Core-only mode and Edge protocol.
2. Establish private connectivity with Tailscale/WireGuard or a tunnel.
3. Add Edge capability health abstraction behind the capability registry.
4. Add Core-side Edge client with auth, retries, idempotency, and backpressure.
5. Move local-only handlers behind Edge capability calls.
6. Add signed URL artifact handoff for Edge-owned raw artifacts.
7. Run Core locally with Edge disconnected and prove text-only flows work.
8. Run Core on VPS with Edge disconnected and prove degraded mode.
9. Reconnect Edge and prove capability recovery.

Each step must keep existing evals green.

## Rejected Alternatives

- **Move everything to the VPS:** rejected because Computer Use, Chrome CDP, and
  authenticated browser workflows depend on local Mac state.
- **Keep Telegram on the Mac:** rejected because the primary availability goal is
  text response while the Mac is off.
- **Use raw SSH from Core to Mac:** rejected because it makes auth, backpressure,
  versioning, and audit lineage harder to control.
- **Expose the Mac with port forwarding or dynamic DNS:** rejected because NAT
  traversal and public inbound access add avoidable fragility and risk.

## Acceptance Criteria

- Core can answer Telegram/Web text messages while Edge is disconnected.
- Core reaches Edge through private mesh networking or outbound-only tunneling,
  with no public inbound ports on the Mac.
- Edge capabilities are explicitly unavailable or degraded with a reason.
- Failed Edge health immediately blocks task dispatch and enters degraded mode.
- Reconnecting Edge restores capability routing without restarting Core.
- A2A contract tests cover auth failure, retry, backpressure, schema mismatch,
  version skew, signed callbacks, and degraded mode.
- Raw Edge artifacts are stored by Edge/object storage and referenced in Core by
  metadata plus signed URL.
- Existing behavior evals and capability registry evals pass in both connected
  and disconnected modes.
