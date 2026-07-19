#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"

chmod +x .githooks/pre-commit .githooks/pre-push
chmod +x .githooks/post-commit .githooks/post-checkout .githooks/post-merge
git config core.hooksPath .githooks

actual="$(git config --get core.hooksPath)"
if [[ "$actual" != ".githooks" ]]; then
  echo "failed to install tracked hooks: core.hooksPath=$actual" >&2
  exit 1
fi

echo "futureproof hooks installed: $actual"
