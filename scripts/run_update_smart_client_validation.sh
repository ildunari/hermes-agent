#!/opt/homebrew/bin/bash
set -euo pipefail

ROOT=/Users/Kosta/LocalDev/.worktrees/hermes-agent/update-client-first-20260710-164740
PY=/Users/Kosta/.hermes/hermes-agent/.venv/bin/python
LOGDIR="$ROOT/.update-smart-validation"
mkdir -p "$LOGDIR"
cd "$ROOT"

exec > >(tee "$LOGDIR/validation.log") 2>&1

echo "[1/7] carry contract"
"$PY" scripts/check_local_carry_contract.py

echo "[2/7] compile/conflict checks"
"$PY" -m py_compile run_agent.py gateway/run.py gateway/session.py hermes_cli/inventory.py hermes_cli/model_switch.py tools/web_tools.py
if grep -R -n -E '^(<<<<<<<|=======|>>>>>>>)' run_agent.py gateway/run.py gateway/session.py hermes_cli/inventory.py hermes_cli/model_switch.py tools/web_tools.py; then
  echo "conflict markers found" >&2
  exit 1
fi

echo "[3/7] session store and profile lineage tests"
"$PY" -m pytest -q \
  tests/gateway/test_session_store_lock_io.py \
  tests/gateway/test_async_session_store.py \
  tests/gateway/test_session.py \
  tests/gateway/test_session_hygiene.py \
  tests/test_tui_gateway_server.py \
  tests/tools/test_delegate.py \
  tests/tools/test_session_search.py \
  tests/agent/test_shell_hooks.py \
  tests/agent/test_session_hygiene_canonical_hook.py

echo "[4/7] model picker/inventory tests"
"$PY" -m pytest -q \
  tests/hermes_cli/test_inventory.py \
  tests/hermes_cli/test_model_switch_custom_providers.py \
  tests/hermes_cli/test_model_picker_policy.py \
  tests/test_model_picker_visibility_policy.py

echo "[5/7] web extraction tests"
"$PY" -m pytest -q \
  tests/tools/test_web_tools_config.py \
  tests/tools/test_web_tools_dict_urls.py

echo "[6/7] Desktop typecheck and focused UI/platform tests"
cd "$ROOT/apps/desktop"
npm run typecheck
npm run test:ui -- --run src/app/session/hooks/use-session-actions.test.tsx src/hermes.test.ts
npm run test:desktop:platforms

echo "[7/7] final source checks"
cd "$ROOT"
git diff --check
git status --short --branch
printf 'VALIDATION_OK commit=%s\n' "$(git rev-parse HEAD)"
