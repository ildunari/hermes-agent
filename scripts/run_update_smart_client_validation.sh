#!/opt/homebrew/bin/bash
set -euo pipefail

ROOT="${HERMES_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)}"
TEST_RUNNER="${HERMES_TEST_RUNNER:-$ROOT/scripts/run_tests.sh}"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif [[ -x "$ROOT/venv/bin/python3" ]]; then
  PY="$ROOT/venv/bin/python3"
elif [[ -x "$HOME/.hermes/hermes-agent/.venv/bin/python" ]]; then
  PY="$HOME/.hermes/hermes-agent/.venv/bin/python"
else
  echo "No checkout-local Python environment found under $ROOT" >&2
  exit 1
fi
LOGDIR="$ROOT/.update-smart-validation"
mkdir -p "$LOGDIR"
cd "$ROOT"

exec > >(tee "$LOGDIR/validation.log") 2>&1

echo "[1/8] carry contract"
"$PY" scripts/carry.py validate

echo "[2/8] forced bytecode refresh and dashboard import contract"
"$PY" -m compileall -q -f agent gateway hermes_cli tools
"$PY" - <<'PY'
import inspect
from hermes_cli import main, web_server

signature = inspect.signature(web_server.start_server)
required = {
    "host",
    "port",
    "open_browser",
    "allow_public",
    "initial_profile",
    "headless",
    "register_instance",
}
missing = required.difference(signature.parameters)
if missing:
    raise SystemExit(
        "dashboard import contract mismatch: "
        f"{inspect.getfile(main)} calls keywords absent from "
        f"{inspect.getfile(web_server)}: {sorted(missing)}"
    )
print(f"DASHBOARD_IMPORT_CONTRACT_OK {signature}")
PY

echo "[3/8] compile/conflict checks"
"$PY" -m py_compile run_agent.py gateway/run.py gateway/session.py hermes_cli/inventory.py hermes_cli/model_switch.py tools/web_tools.py
if grep -R -n -E '^(<<<<<<<|=======|>>>>>>>)' run_agent.py gateway/run.py gateway/session.py hermes_cli/inventory.py hermes_cli/model_switch.py tools/web_tools.py; then
  echo "conflict markers found" >&2
  exit 1
fi

echo "[4/8] session store and profile lineage tests"
# Keep the large gateway-server suite out of the parallel group. It normally
# finishes in under a minute by itself, but repeatedly stalled at ~80% while
# sharing the validation host with another pytest subprocess and hit the
# per-file timeout. Serial isolation removes that resource-contention path
# without weakening coverage or globally slowing the test runner.
"$TEST_RUNNER" -j 1 tests/test_tui_gateway_server.py -q
"$TEST_RUNNER" -j 2 \
  tests/gateway/test_session_store_lock_io.py \
  tests/gateway/test_async_session_store.py \
  tests/gateway/test_session.py \
  tests/gateway/test_session_hygiene.py \
  tests/tools/test_delegate.py \
  tests/tools/test_session_search.py \
  tests/agent/test_shell_hooks.py \
  tests/agent/test_session_hygiene_canonical_hook.py -q

echo "[5/8] model picker/inventory tests"
"$TEST_RUNNER" -j 2 \
  tests/hermes_cli/test_inventory.py \
  tests/hermes_cli/test_model_switch_custom_providers.py \
  tests/hermes_cli/test_model_picker_policy.py \
  tests/test_model_picker_visibility_policy.py -q

echo "[6/8] web extraction tests"
"$TEST_RUNNER" -j 2 \
  tests/tools/test_web_tools_config.py \
  tests/tools/test_web_tools_dict_urls.py -q

echo "[7/8] Desktop typecheck and focused UI/platform tests"
# UPDATE_CHANGED_DESKTOP=0 is set by the update service when the merge diff
# touched neither apps/desktop/ nor web/; UPDATE_VALIDATION_FULL=1 overrides
# every scoping decision. Unset means run everything (manual invocation).
if [[ "${UPDATE_VALIDATION_FULL:-0}" != "1" && "${UPDATE_CHANGED_DESKTOP:-1}" == "0" ]]; then
  echo "SKIPPED: diff touched neither apps/desktop/ nor web/"
else
  cd "$ROOT/apps/desktop"
  npm run typecheck
  # Run the FULL desktop UI suite, not a hand-picked pair. The old two-file
  # scope gated ~6,500 tests on 2 files, so real breakage (2026-08-19: 14
  # failures across preview-pane and model-menu-panel) sailed through every
  # update invisibly. A known-failing baseline is tolerated via
  # UPDATE_DESKTOP_UI_BASELINE so pre-existing upstream redness doesn't block
  # a merge, while any NEW failure still fails the gate.
  ui_status=0
  npm run test:ui -- --run > /tmp/update-smart-ui.log 2>&1 || ui_status=$?
  tail -40 /tmp/update-smart-ui.log

  # Parse the "Tests  N failed | ..." summary line specifically. Matching a
  # bare "N failed" also hits the "Test Files" line above it and yields the
  # wrong number, which would let real regressions pass the gate.
  ui_failed="$(grep -oE '^[[:space:]]*Tests[[:space:]]+[0-9]+ failed' /tmp/update-smart-ui.log | grep -oE '[0-9]+' | tail -1 || true)"
  ui_failed="${ui_failed:-0}"
  baseline="${UPDATE_DESKTOP_UI_BASELINE:-0}"

  if [[ "$ui_status" != "0" ]]; then
    if [[ "$ui_failed" -gt 0 && "$ui_failed" -le "$baseline" ]]; then
      echo "DESKTOP UI: $ui_failed failed, at/below known baseline ($baseline) — not blocking"
    else
      echo "DESKTOP UI FAILED: $ui_failed failed vs baseline $baseline"
      exit "$ui_status"
    fi
  fi

  npm run test:desktop:platforms
fi

if [[ "${UPDATE_VALIDATION_FULL:-0}" == "1" ]]; then
  echo "[full] escalated: full python test suite"
  # High-collision merges (LLM resolver used, core-dir conflicts, dependency
  # manifests) are exactly where scoped carry/curated tests miss regressions
  # (2026-07-23 runs 5-6). Full escalation runs the whole python suite.
  cd "$ROOT"
  "$TEST_RUNNER" tests/
fi

echo "[8/8] final source checks"
cd "$ROOT"
git diff --check
git status --short --branch
printf 'VALIDATION_OK commit=%s\n' "$(git rev-parse HEAD)"
