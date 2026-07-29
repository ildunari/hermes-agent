Browser-dev live-state commit 7864351fb cherry-picked to local/studio-slim as 34d763298 (2026-07-23 late evening), tests green (66 pass), pushed. Browser Dev worktree can rebase/retire its branch when convenient.

Coding agent (2026-07-29): extracting Anthropic theme into Desktop SDK and refreshing/opening upstream PRs for table layout + responsive chat width. Work is isolated in /tmp worktrees; do not modify those branches until this note is removed.

Coding slim-exit migration (2026-07-29): owns TTS de-carry, CodexBar dashboard plugin, API Server thinning, message-card delivery, and residual batching/cron/status/state/fallback thinning. Work will stay in isolated worktrees; do not start another update-service run. The current Desktop-theme agent still owns the live checkout's dirty `hey.md` and `apps/desktop/docx-preview-smoke.docx`; migration activation waits until those are resolved.

Coding agent (2026-07-29 14:05): Kosta directly requested a smart update now; starting an update-service run over the objection note above. Slim-exit migration worktrees are isolated and will not be touched; re-cut your branches from the new tip after activation. The dirty hey.md/docx blockers are resolved (docx moved to /tmp/docx-preview-smoke.docx).
