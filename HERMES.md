# Hermes Agent Local Overlay

This file is the repo-cwd project context for Hermes sessions started inside this checkout.

When this file is present, repo-root `AGENTS.md` is not auto-loaded by Hermes project-context discovery. Read `./AGENTS.md` for repo engineering rules before changing code.

Shared cross-profile operating rules live in `~/.config/hermes-state/shared/instructions-library/`; do not duplicate them here.

Do not push update-slim work or restart the live gateway unless Kosta explicitly asks. The live gateway runs from `/Users/Kosta/.hermes/hermes-agent`, so use separate worktrees for branch work.

## Multi-Agent Collaboration

Other models may be working in this project at the same time. If anything odd happens or files change unexpectedly, assume another agent may be responsible before reverting or overwriting it. Use `hey.md` in the project root to coordinate with the other agents. Never block waiting for them: leave a concise message, keep making progress on unblocked work, reconcile changes constructively, and work together to achieve the shared goal. When the work is complete, remove resolved coordination messages from `hey.md`; delete the file if it is empty.
