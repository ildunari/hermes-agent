#!/usr/bin/env bash
# Idempotent local merge aids for the thin carry branch.
# - rerere + autoupdate + zdiff3
# - shareable rr-cache under ~/.hermes/shared/git-rr-cache/hermes-agent
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
SHARED_RR="${HERMES_RR_CACHE:-$HOME/.hermes/shared/git-rr-cache/hermes-agent}"
mkdir -p "$SHARED_RR"

git config rerere.enabled true
git config rerere.autoupdate true
git config merge.conflictStyle zdiff3

if [ -L .git/rr-cache ]; then
  target="$(readlink .git/rr-cache)"
  if [ "$target" != "$SHARED_RR" ]; then
    echo "note: .git/rr-cache already symlinked to $target (left in place)"
  fi
elif [ -d .git/rr-cache ]; then
  # Seed shared cache if empty, then switch this clone to the shared path.
  if [ -z "$(ls -A "$SHARED_RR" 2>/dev/null || true)" ]; then
    rsync -a --link-dest="$ROOT/.git/rr-cache/" "$ROOT/.git/rr-cache/" "$SHARED_RR/"
  else
    # Merge any local-only resolutions into the shared cache.
    rsync -a "$ROOT/.git/rr-cache/" "$SHARED_RR/"
  fi
  ts=$(date +%Y%m%d-%H%M%S)
  mv .git/rr-cache ".git/rr-cache.local-backup-$ts"
  ln -s "$SHARED_RR" .git/rr-cache
  echo "linked .git/rr-cache -> $SHARED_RR (backup .git/rr-cache.local-backup-$ts)"
elif [ ! -e .git/rr-cache ]; then
  ln -s "$SHARED_RR" .git/rr-cache
  echo "created .git/rr-cache -> $SHARED_RR"
fi

echo "merge aids ready:"
echo "  rerere.enabled=$(git config --get rerere.enabled)"
echo "  rerere.autoupdate=$(git config --get rerere.autoupdate)"
echo "  merge.conflictStyle=$(git config --get merge.conflictStyle)"
echo "  rr-cache=$(readlink .git/rr-cache 2>/dev/null || echo .git/rr-cache)"
echo "Cross-machine: rsync -a --delete \$HOME/.hermes/shared/git-rr-cache/ other-mac:~/.hermes/shared/git-rr-cache/"
