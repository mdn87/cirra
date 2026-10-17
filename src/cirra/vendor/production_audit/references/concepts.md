# Production-Audit Concept Catalog

The 111 concepts come from the "Who's gonna tell vibe coders about…" list
(@nataindata, 2026-10). IDs `C001`–`C111` keep the original numbering so a
finding can always be traced back to the list. `X01`–`X06` are supplements the
list leaves out but a production audit cannot skip; they are marked as such.

Columns:

- **Tiers**: `P` every-PR (only when the diff touches the area), `F` full
  audit (every concept is in F), `R` release readiness.
- **Applies when**: the repository signal that makes the concept relevant.
  If no signal is present, the concept is `not_applicable` and the reason
  cites the profile.
- **Verify**: what implementation evidence looks like. A config key, README
  claim, or dependency in a lockfile is a lead, not evidence; evidence is the
  code path or deploy artifact that exercises it. The exception is a
  process concept whose Verify cell asks for a document (DR plan, runbook,
  on-call rota, SLO, postmortem): there the committed document is the
  artifact, so a range in it counts as evidence. A README sentence claiming
  the process exists still does not.
- **If missing**: the concrete consequence, which drives severity.

Several entries are knowledge concepts (CAP theorem, P99 latency, TCP vs UDP).
For those, the Verify column turns the concept into a checkable question about
the code.

## 1. Reliability and resilience

| ID | Concept | Tiers | Applies when | Verify | If missing |
|---|---|---|---|---|---|
| C010 | Circuit breakers | P F | Synchronous calls to remote services that can fail or hang | Breaker (library or hand-rolled) wraps the client; open state short-circuits and recovers | One slow dependency exhausts workers and cascades into a full outage |
| C011 | Timeouts | P F R | Any network, DB, subprocess, or lock wait | Explicit connect/read timeouts on every client and subprocess call; no unbounded `wait()`/`recv()` | Hung calls pin threads or processes forever; silent stalls |
| C012 | Retries | P F | Calls to remote or flaky resources | Retries bounded by count or deadline, only on retryable errors, and only for idempotent operations | Transient blips become user-facing failures, or unbounded retries amplify an outage |
| C013 | Exponential backoff | P F | Any retry loop | Delay grows per attempt with jitter and a cap | Retry storms synchronize and hammer a recovering dependency |
| C014 | Idempotency | P F | Retried requests, webhook/queue consumers, payment or state-changing endpoints | Idempotency keys, upserts, dedupe tables, or naturally idempotent writes on every retried path | Duplicate side effects: double charges, double sends, duplicate rows |
| C044 | Backpressure | F | Producers that can outrun consumers: queues, streams, worker pools | Bounded queues/channels, rejection or flow control when full | Memory blowup or latency collapse under load |
| C083 | Disaster recovery | F R | Any persistent state the service cannot recreate | Documented and rehearsed restore path with RPO/RTO; restore scripts exist | Regional or host loss becomes permanent data loss |
| C084 | Backups | F R | Databases, object stores, or files holding non-derivable data | Scheduled backup job or managed backup config, plus a tested restore | One bad migration or disk failure destroys data |
| C085 | Failover | F R | A single instance whose loss stops service, and the project has an availability need | Standby/replica with promotion path, or explicit acceptance of the single point of failure | Host loss equals outage until manual rebuild |
| C087 | Chaos engineering | F | Multi-service or HA systems that claim fault tolerance | Fault-injection tests or drills exercising the failure paths | Failure handling is untested and fails on first real use |
| C095 | Network partitions | F | Distributed state across hosts or services | Defined behavior when peers are unreachable: fencing, quorum, degraded mode | Split brain, divergent state, or indefinite hangs |

## 2. Traffic, caching, and edge

| ID | Concept | Tiers | Applies when | Verify | If missing |
|---|---|---|---|---|---|
| C001 | Rate limiting | P F R | Public or multi-tenant endpoints, login, expensive operations, outbound API quotas | Limiter in middleware, gateway, or proxy config; outbound clients respect provider limits | Abuse, credential stuffing, runaway cost, or bans from upstream APIs |
| C002 | Caching | F | Repeated expensive reads or remote calls | Cache with explicit TTL and bounded size | Avoidable latency and load; or, done badly, unbounded memory |
| C003 | Load balancing | F | More than one instance of a service | LB config with health-check-based routing | Uneven load; dead instances keep receiving traffic |
| C004 | Reverse proxies | F R | HTTP services exposed beyond localhost | Proxy terminates TLS, sets timeouts and body limits, forwards real client IP safely | App server exposed directly; header spoofing; no request limits |
| C005 | API gateways | F | Several public services sharing auth, quotas, or routing | Gateway config centralizes auth/limits, or a reason it is not needed | Cross-cutting concerns reimplemented inconsistently per service |
| C009 | Service discovery | F | Services locate each other dynamically | Registry, DNS-based discovery, or orchestrator service names instead of hardcoded hosts | Hardcoded IPs break on every move or restart |
| C048 | CDN | F | Static assets or downloads served to remote users | CDN in front of static content with cache headers | Origin overload and slow global access |
| C049 | Edge caching | F | Cacheable responses served across regions | Cache-Control/surrogate keys set deliberately per route | Stale or private data cached at the edge, or no caching at all |
| C050 | Cache invalidation | P F | Any cache whose source data changes | Writes invalidate or version the cached keys; TTLs match staleness tolerance | Users see stale or wrong data indefinitely |

## 3. Messaging and asynchronous work

| ID | Concept | Tiers | Applies when | Verify | If missing |
|---|---|---|---|---|---|
| C015 | Message queues | F | Work deferred or handed between processes | Durable queue with ack semantics; consumers ack after success | Lost jobs on crash; work done twice or never |
| C016 | Pub/Sub | F | One event fans out to several consumers | Topic/subscription model with per-consumer delivery state | Tight coupling; consumers miss events |
| C017 | Event-driven architecture | F | State changes trigger downstream work | Events have schemas, versions, and ordering/duplication handling | Implicit coupling and undebuggable chains of side effects |
| C018 | Distributed transactions | F | One operation writes to more than one system of record | Outbox, two-phase commit, or explicit compensation | Partial writes leave systems permanently inconsistent |
| C019 | Saga pattern | F | Multi-step workflows across services that must roll back | Each step has a compensating action; saga state persisted | Half-finished workflows with no recovery path |
| C020 | Dead letter queues | P F R | Queue or webhook consumers | Poison messages move to a DLQ after N failures; DLQ is monitored | One bad message blocks the queue or is silently dropped |
| C021 | Cron jobs | P F R | Scheduled tasks (cron, systemd timers, Task Scheduler, CI schedules) | Jobs are idempotent, overlap-safe (lock or single-flight), logged, and alert on failure | Silent failed runs; overlapping runs corrupt data |
| C022 | WebSockets | F | Bidirectional real-time connections | Auth on connect, heartbeat/ping, reconnect with backoff, per-connection limits | Leaked connections, unauthenticated channels, reconnect storms |
| C023 | Long polling | F | Clients wait on server for updates over HTTP | Bounded wait, timeouts aligned with proxies, resume cursor | Proxy timeouts, missed updates, thread exhaustion |
| C024 | Server-Sent Events | F | One-way server push over HTTP | Last-Event-ID resume, heartbeats, proxy buffering disabled | Silent disconnects and lost events |
| C101 | Webhooks | P F R | Sending or receiving webhooks | Inbound: signature verification, replay window, idempotent handling, fast ack. Outbound: retries with backoff, timeouts | Forged events accepted, duplicate processing, or dropped deliveries |

## 4. Data and storage

| ID | Concept | Tiers | Applies when | Verify | If missing |
|---|---|---|---|---|---|
| C025 | Database indexing | P F | Queries filter, join, or sort on columns | Indexes exist for hot query predicates (schema or migrations) | Full scans; latency grows with data |
| C026 | Query optimization | F | Non-trivial queries on growing tables | Queries select needed columns, paginate, avoid per-row work; EXPLAIN evidence or tests on size | Slow endpoints and DB saturation |
| C027 | N+1 queries | P F | ORM or loop-driven data access | Eager loading/batching where loops touch relations | Request cost scales with result size; latency spikes |
| C028 | Connection pooling | P F | Any DB or HTTP client used repeatedly | Shared pool with size limits; connections returned on all paths | Connection exhaustion; per-request handshake cost |
| C029 | Read replicas | F | Read-heavy load on a primary DB | Read routing that tolerates replica lag | Primary overload; or stale reads where consistency was required |
| C030 | Sharding | F | Data or write load exceeds one node | Shard key and routing, or a stated reason it is unnecessary at current scale | Usually not applicable; when it applies, hot shards and painful resharding |
| C031 | Partitioning | F | Large tables with time or tenant locality | Table/partition strategy with retention | Unbounded table growth and slow maintenance |
| C032 | Replication | F R | Data must survive node loss | Replication configured and monitored for lag | Single-node data loss |
| C034 | CAP theorem | F | Replicated or distributed data | The system documents and implements which side it takes during partitions | Undefined behavior under partition; contradictory guarantees |
| C035 | Eventual consistency | F | Async replication, caches, or event-propagated state | Code tolerates stale reads; UI and callers do not assume read-your-writes where it is absent | Intermittent "missing" data bugs that cannot be reproduced |
| C081 | Database migrations | P F R | A schema exists | Versioned migration files applied by a tool; migrations are forward-safe and reversible or have a rollback plan | Hand-edited schemas drift between environments; deploys break data |
| C082 | Schema versioning | P F R | Persisted or exchanged data formats (DB, files, messages, APIs) | Version fields or migration history; readers handle old versions | Old data becomes unreadable after an upgrade |
| X04 | Database transactions *(supplement)* | P F | Multi-statement writes that must succeed or fail together | Writes grouped in transactions with correct isolation; no partial-commit paths | Partial updates corrupt invariants on any error |
| X06 | Storage growth and retention *(supplement)* | P F R | Anything that writes files, logs, rows, history, or artifacts on a recurring basis | Every recurring write path has a bound: rotation, TTL, pruning job, or overwrite-in-place | Disk or directory fills over weeks; the service fails long after the change that caused it |

## 5. Concurrency and runtime

| ID | Concept | Tiers | Applies when | Verify | If missing |
|---|---|---|---|---|---|
| C033 | Leader election | F | Several instances where only one may run a duty | Lease/lock-based election with expiry | Duplicate schedulers or none at all |
| C036 | Optimistic locking | P F | Concurrent updates to the same records with low contention | Version column or ETag checked on write | Lost updates: last write silently wins |
| C037 | Pessimistic locking | P F | High-contention or must-not-interleave updates | Row/advisory locks held for minimal scope with timeouts | Lost updates, or long lock holds causing stalls |
| C038 | Distributed locks | P F | Mutual exclusion across processes or hosts | Lock with TTL and fencing token; release on all paths | Two holders at once after expiry; stuck locks after crash |
| C039 | Race conditions | P F | Shared mutable state, check-then-act, file or DB read-modify-write | Atomic operations, locks, or unique constraints at each check-then-act site | Intermittent corruption and duplicate processing |
| C040 | Deadlocks | P F | Multiple locks, nested transactions, or blocking waits between workers | Consistent lock ordering; lock timeouts; no blocking calls while holding locks | Hard hangs requiring restarts |
| C041 | Memory leaks | P F | Long-running processes | Bounded caches/collections, closed resources, listeners removed | Gradual degradation until OOM kill |
| C042 | Garbage collection | F | GC runtimes with latency-sensitive paths or large heaps | Allocation-heavy hot paths reviewed; GC/heap settings intentional | Latency spikes from GC pauses |
| C043 | Thread safety | P F | Threads, async tasks, or shared clients across workers | Shared state guarded or immutable; non-thread-safe clients not shared | Data races and heisenbugs |
| C096 | Clock skew | F | Correctness depends on comparing timestamps across hosts, token expiry, or ordering | Monotonic clocks for durations; tolerance windows on expiry; no cross-host wall-clock ordering | Rejected valid tokens, misordered events, broken leases |

## 6. Scaling and performance

| ID | Concept | Tiers | Applies when | Verify | If missing |
|---|---|---|---|---|---|
| C045 | Autoscaling | F | Variable load on a horizontally scalable service | Scaling policy tied to a real signal, with min/max bounds | Over-provisioning cost or under-provisioned outages |
| C046 | Horizontal scaling | F | Load may exceed one instance | Stateless request handling; shared state externalized | Cannot add capacity without a rewrite |
| C047 | Vertical scaling | F | Single-instance services | Resource limits known; headroom measured | Surprise OOM or CPU saturation |
| C088 | Cost optimization | F | Paid cloud or API usage | Usage bounded (quotas, limits, budgets, alerts); idle resources cleaned up | Runaway bills from loops, retries, or forgotten resources |
| C089 | Cold starts | F | Serverless or scale-to-zero deployments | Init work minimized; latency budget accounts for cold start | First-request timeouts and user-visible latency |
| C090 | Serverless limits | F | Serverless functions | Execution time, payload, concurrency, and memory limits respected in code | Truncated jobs and throttling failures |
| C091 | Latency | F R | User-facing or SLA-bound paths | Latency measured on key paths | Slowness discovered by users first |
| C092 | Throughput | F | Batch, streaming, or high-volume endpoints | Throughput measured or load-tested against expected volume | Backlogs grow without bound |
| C093 | P99 latency | F R | Latency-sensitive services with real traffic | Percentiles, not averages, recorded and alerted on | Averages hide the requests users actually complain about |
| C094 | Tail latency | F | Fan-out requests that wait on the slowest call | Hedging, per-call timeouts, or fan-out limits | One slow dependency sets every request's latency |

## 7. Delivery and deployment

| ID | Concept | Tiers | Applies when | Verify | If missing |
|---|---|---|---|---|---|
| C006 | CI/CD | F R | Any shared codebase | Pipeline runs tests and checks on every change; deploy path is scripted, not manual | Broken code reaches main; deploys are irreproducible |
| C007 | Docker | F | Containerized deploys | Pinned base images, non-root user, minimal layers, healthcheck, no secrets baked in | Unreproducible images, bloated attack surface, leaked secrets |
| C008 | Kubernetes | F | K8s manifests or charts present | Resource requests/limits, probes, PodDisruptionBudgets, non-root security context | Evictions, noisy neighbors, unsafe pods |
| C051 | Feature flags | F R | Risky features shipped to live users | Flags with owners and cleanup; safe defaults when the flag service is down | Big-bang releases with no kill switch |
| C052 | Blue-green deployment | F R | Zero-downtime requirement with switchable environments | Two environments with an atomic traffic switch | Deploys cause downtime |
| C053 | Canary releases | F R | Large user base where partial rollout limits blast radius | Traffic split with automated health gates | Bad releases hit everyone at once |
| C054 | Rolling deployments | F R | Multi-instance services | Rollout keeps capacity and waits on health checks | Deploys drop capacity or ship broken instances |
| C055 | Rollbacks | P F R | Anything deployed | Documented one-step rollback to the previous artifact; migrations compatible with rollback | A bad deploy cannot be undone quickly |
| C086 | Multi-region deployment | F | Stated multi-region availability or latency requirement | Region-aware data and failover | Usually not applicable; when required, regional outage equals total outage |
| C102 | API versioning | P F | APIs consumed by clients you do not deploy in lockstep | Versioned routes/headers; breaking changes go to a new version | Client breakage on every change |
| C103 | Semantic versioning | P F R | Published libraries, packages, or plugins | Version bumps match change type; changelog maintained | Consumers break on "minor" upgrades |
| C104 | Infrastructure as code | F R | Any provisioned infrastructure | Infra defined in versioned code (Terraform, Ansible, scripts, compose) instead of manual console changes | Snowflake servers that cannot be rebuilt |
| C105 | Terraform | F | Terraform present | Remote state with locking, pinned providers, no secrets in state outputs, plan reviewed in CI | State corruption, drift, leaked secrets |
| C106 | Helm charts | F | Helm charts present | Values validated, pinned chart versions, probes and limits templated | Inconsistent releases across environments |
| C107 | Build caching | F | Slow builds or CI | Cache keys tied to lockfiles/inputs; cache cannot poison release builds | Slow feedback; or stale artifacts shipped |
| C108 | Dependency hell | P F R | Any third-party dependencies | Lockfiles committed, versions pinned, update and vulnerability scanning in place | Unreproducible builds and unpatched vulnerabilities |
| X03 | Automated tests *(supplement)* | P F R | Any code with behavior worth keeping | Tests cover critical paths and run in CI; changed behavior has tests | Regressions ship silently |

## 8. Observability and operations

| ID | Concept | Tiers | Applies when | Verify | If missing |
|---|---|---|---|---|---|
| C056 | Health checks | F R | Any long-running service | Health endpoint or check that exercises real dependencies, used by the supervisor/LB | Dead services keep receiving traffic; failures go unnoticed |
| C057 | Liveness and readiness probes | F R | Orchestrated or supervised services | Separate liveness (restart me) and readiness (route to me) signals | Restart loops or traffic to services still warming up |
| C058 | Monitoring | F R | Anything running unattended | Dashboards or checks on service health and resources | Outages discovered by users |
| C059 | Logging | P F R | Any running service or job | Structured logs with levels and request/job IDs; no secrets or PII in logs | Undebuggable incidents; credential leaks via logs |
| C060 | Distributed tracing | F | Requests crossing several services | Trace context propagated across hops | Cross-service latency and failures cannot be localized |
| C061 | Metrics | F R | Services with load, errors, or queues | Counters/histograms for rate, errors, duration, queue depth | No way to see degradation before failure |
| C062 | Alerts | F R | Unattended production services | Alerts on symptoms users feel, routed to a human | Failures sit unnoticed for days |
| C063 | SLOs | F R | Services with users who depend on them | Written objectives for availability/latency | No shared definition of "working" |
| C064 | SLIs | F R | SLOs exist or are needed | Indicators measured from real traffic | SLOs cannot be evaluated |
| C065 | Error budgets | F | SLOs exist | Budget policy that gates releases when burned | Reliability work never gets prioritized |
| C066 | Observability | F R | Any production system | Logs, metrics, and traces together answer "why is this broken" without redeploying | Every incident starts with adding print statements |
| C109 | Production incidents | F R | Systems with real users | Incident process: severity levels, communication, ownership | Chaotic, slow response |
| C110 | On-call | F R | Systems that must be fixed outside business hours | Rotation or named owner with runbooks | Nobody responds when it breaks at night |
| C111 | Postmortems | F R | Past incidents exist | Blameless postmortems with tracked action items | The same incident repeats |
| X05 | Error tracking *(supplement)* | F R | Any running application | Exceptions captured with context (Sentry or equivalent, or aggregated error logs) | Errors happen silently in production |

## 9. Security

| ID | Concept | Tiers | Applies when | Verify | If missing |
|---|---|---|---|---|---|
| C067 | Secrets management | P F R | Any credential, key, or token | Secrets from env/secret store, never committed; `.env` ignored; rotation possible | Leaked credentials in git history or images |
| C068 | IAM | P F R | Cloud resources, service accounts, or multi-user systems | Least-privilege roles; no wildcard or root credentials in apps | One compromised component owns everything |
| C069 | OAuth | P F | Delegated auth or third-party login | State/PKCE used, redirect URIs exact-matched, tokens stored safely | Account takeover via redirect or CSRF attacks |
| C070 | JWT rotation | P F | JWTs issued or verified | Signing keys rotatable (kid), algorithm pinned, expiry enforced, revocation strategy | Forged or eternal tokens; painful key compromise response |
| C071 | TLS | P F R | Network traffic beyond localhost | TLS on external endpoints; certificate verification never disabled in clients | Credential interception; MITM |
| C072 | Encryption at rest | F R | Sensitive data stored | Disk/DB/object encryption or field-level encryption for sensitive fields | Data exposure from stolen disks, backups, or snapshots |
| C073 | Encryption in transit | P F R | Data crosses a network, including internal hops | TLS or mTLS on internal service calls carrying sensitive data | Lateral sniffing inside the network |
| C074 | WAF | F | Public web applications | WAF or equivalent request filtering in front of the app, or justified absence | Commodity attacks reach the app unfiltered |
| C075 | DDoS protection | F | Public endpoints | Upstream protection (CDN/proxy) and rate limits | Trivial outage by traffic flood |
| C076 | CORS | P F | Browser-facing APIs | Explicit origin allowlist; no `*` with credentials | Cross-origin data theft |
| C077 | CSRF | P F | Cookie-authenticated state-changing requests | CSRF tokens or SameSite cookies plus origin checks | Attackers perform actions as logged-in users |
| C078 | SQL injection | P F | Any SQL built in code | Parameterized queries only; no string-formatted SQL with input | Database compromise |
| C079 | XSS | P F | HTML rendered with user-influenced data | Output encoding/templating auto-escape; no raw HTML sinks with input; CSP | Session theft and account takeover |
| C080 | SSRF | P F | Server fetches URLs or hosts influenced by input | Allowlists, blocked private/metadata ranges, no redirects to internal hosts | Internal network and cloud metadata exposure |
| X01 | Input validation *(supplement)* | P F | Any external input: HTTP, CLI, files, messages | Schema validation at trust boundaries; size limits; path traversal and command injection guarded | Injection, crashes, and corrupted state from malformed input |
| X02 | Authentication and authorization *(supplement)* | P F R | Any endpoint or tool not meant for everyone | Every route/handler checks identity and permission server-side; default deny | Unauthenticated or cross-tenant access |

## 10. Networking and protocols

| ID | Concept | Tiers | Applies when | Verify | If missing |
|---|---|---|---|---|---|
| C097 | DNS | F R | Services resolved by name, custom domains, or tunnels | Records managed in code or documented; TTLs sane; resolution failures handled | Outages from record mistakes or stale caches |
| C098 | TCP vs UDP | F | Custom network protocols | Transport choice matches reliability/ordering needs; UDP paths handle loss | Lost or reordered data on a protocol that assumed reliability |
| C099 | HTTP/2 and HTTP/3 | F | High-concurrency HTTP services or clients | Protocol support configured at the proxy/server; clients reuse connections | Head-of-line blocking and connection overhead |
| C100 | gRPC | F | gRPC services or clients | Deadlines on every call, versioned protos, TLS, retry policy | Hung calls and breaking proto changes |
