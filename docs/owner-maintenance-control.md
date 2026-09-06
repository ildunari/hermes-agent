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
