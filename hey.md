Browser-dev live-state commit 7864351fb cherry-picked to local/studio-slim as 34d763298 (2026-07-23 late evening), tests green (66 pass), pushed. Browser Dev worktree can rebase/retire its branch when convenient.

Coding agent (2026-07-29): extracting Anthropic theme into Desktop SDK and refreshing/opening upstream PRs for table layout + responsive chat width. Work is isolated in /tmp worktrees; do not modify those branches until this note is removed.

Coding slim-exit migration (2026-07-29): owns TTS de-carry, CodexBar dashboard plugin, API Server thinning, message-card delivery, and residual batching/cron/status/state/fallback thinning. Work will stay in isolated worktrees; do not start another update-service run. The current Desktop-theme agent still owns the live checkout's dirty `hey.md` and `apps/desktop/docx-preview-smoke.docx`; migration activation waits until those are resolved.

Coding agent (2026-07-29 14:05): Kosta directly requested a smart update now; starting an update-service run over the objection note above. Slim-exit migration worktrees are isolated and will not be touched; re-cut your branches from the new tip after activation. The dirty hey.md/docx blockers are resolved (docx moved to /tmp/docx-preview-smoke.docx).

Coding smart update (2026-08-26): pinned upstream merge is verified and being re-run after a transient DMG detach failure. This update also changes `gateway/run.py`; the texture de-carry lane must rebase on the new live tip and reconcile that file after activation rather than landing from the old base.

Coding plugin de-carry program (2026-08-31): four isolated worktree lanes own web-wrapper, gateway-command, provider-policy, and Desktop-enhancement extraction. Do not edit their `decarry/*-20260831` branches or the paired worktrees under `~/LocalDev/.worktrees/{hermes-agent,hermes-kosta-plugins}/`. Parent integration will cherry-pick verified commits onto current `local/studio-slim` and plugin `main`; no live restart/update-service run until the integrated review gate closes.
