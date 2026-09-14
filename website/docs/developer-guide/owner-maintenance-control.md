# Serve and compute owner maintenance

Protocol version 1 uses the existing dashboard drain-secret Bearer provider
(`HERMES_DASHBOARD_DRAIN_SECRET`, `drain` scope). No cookie-only or unauthenticated
loopback bypass exists. An unavailable provider/owner is a refusal, not idle proof.

POST `/api/maintenance/status` with `{}` returns `{protocol_version: 1, owners: [...]}`.
Each owner contains `owner_pid`, `owner_generation`, `owner_kind` (`serving` or
`compute_host`), `hermes_home`, `request_token`, `released_request_token`,
`admissions_closed`, `bootstrap_held`, `active_turns`, `queued_turns`,
`delegation_count`, and `pending_approvals`. A generation is a process-lifetime
UUID; for compute children it is also the native supervisor hello `boot_id`.
Status may optionally pin `owner_generation`; a stale generation returns 409.

POST `/api/maintenance/begin` with `{owner_generation, request_token}` closes
admission and persists the transaction token before acknowledging. Repeating the
same request is safe. Another token or stale generation returns 409. Existing
turns, Stop, and approval responses continue; queued user work stays in its native
queue. Previously admitted executor work drains normally. Status includes sessions
whose clients disconnected. Delegation count is a conservative liveness count:
the maximum of native live child records and async delegation units (which overlap),
not a billing count or sum of messaging activity.

The bootstrap fence is `<hermes_home>/runtime/owner-maintenance.json`, protected
by `owner-maintenance.lock` using the native active-session file lock. It stores
only the request token and protocol version, not an owner/session registry. New
owners read it before first admission, including a lazily created compute child.
A malformed/unreadable fence refuses startup/admission. Do not delete it to bypass
an unsupported runtime. Multiple serving endpoints sharing a home require support
to verify **all** affected endpoints before releasing or finalizing any of them.

POST `/api/maintenance/release` with the same generation/token opens that owner
in memory and retains the bootstrap fence. Active work need not be idle to cancel
a preactivation transaction. An untouched open owner may acknowledge cancellation
when the matching persisted token exists. Releases retain `released_request_token`
for retries after a lost response.

Release pumps native queued work once the serving and compute owners are open.
Repeated release cannot duplicate a claimed prompt. A goal continuation held during
maintenance stays on its native session and contributes to queued work; dispatch
rechecks the native persisted GoalManager and Stop's queue generation, so paused
or cancelled goals are not revived. User messages retain priority over goals.

After verifying every replacement and releasing every owner, repeat release with
`finalize_bootstrap: true` and `expected_owner_generations: [...]` containing the
exact current endpoint inventory. Every owner must be open and released under the
same token. The native supervisor pins the child against respawn while clearing
the bootstrap fence. Re-read status afterward. Partial failure leaves the fence
for explicit recovery. Do not begin a new transaction before the previous one is
fully released/finalized.

The endpoint never starts a missing child to manufacture idle proof; an existing
but unreachable child returns 503. It covers this serving process and its existing
compute child only. Messaging gateways, cron owners, standalone TUI/PTY owners,
and older runtimes are **unsupported** here. Support must match every affected PID
to a supported owner or defer; zero messaging counters are not a desktop barrier.
The initial legacy transition and production activation remain lead-owned.

## Native messaging gateway and profile ticker

Messaging uses the existing private gateway socket, not the HTTP endpoint above:
`query_gateway_control(Path(home), "owner_maintenance", body={...})` accepts
`action: status|begin|release` and returns an unwrapped protocol-version-1
`owners` list with `owner_kind: gateway`. `None` is a refusal, never idle proof.
Begin/release require the exact `owner_generation` and `request_token`; final
release adds `finalize_bootstrap: true` and
`expected_owner_generations: [current_generation]`.

The gateway's versioned `.drain_request.json` survives replacement and does not
expire. Its control socket starts before held adapter startup, so a replacement
can acknowledge maintenance before normal transport readiness. Startup and cron
pre-registration reservations count as active work. Held turns retain their
native caller/queue and recheck Stop generations on release. Unsupported cron
providers refuse maintenance rather than claim coverage. Legacy dashboard drain
expiry remains unchanged outside a versioned transaction.

The support-owned profile ticker is a separate lock gate, not a resident process
owner. Its `maintenance_control(action, body, root_home=...)` reports its
import-captured `code_sha256`, full native profile allowlist, lock/active receipts,
`covered_pids`, `quiescent`, and admission/bootstrap state. Begin requires a token;
release/finalization additionally require the executing source hash. The marker
is `<root>/state/profile-cron-ticker.maintenance.json`. Busy unregistered locks,
malformed or legacy receipts, and unknown active children never prove idleness.
Controllers must execute a hash-pinned helper; choosing a state root does not
choose or attest its code. Live legacy bootstrap still requires separate proof.
