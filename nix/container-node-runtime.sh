#!/bin/sh
# Sourced by the NixOS OCI entrypoint. The locked, hash-verified Nix derivation
# is mounted read-only into the container, so this exact runtime cannot float.

hermes_activate_immutable_node() {
    : "${HERMES_CONTAINER_NODE_BIN:?HERMES_CONTAINER_NODE_BIN must be set}"
    : "${HERMES_CONTAINER_NODE_VERSION:?HERMES_CONTAINER_NODE_VERSION must be set}"

    node_bin="$HERMES_CONTAINER_NODE_BIN/node"
    if [ ! -x "$node_bin" ]; then
        echo "ERROR: immutable Hermes Node is missing: $node_bin" >&2
        return 1
    fi
    actual_version=$("$node_bin" --version 2>/dev/null) || {
        echo "ERROR: immutable Hermes Node does not run: $node_bin" >&2
        return 1
    }
    actual_version=${actual_version#v}
    if [ "$actual_version" != "$HERMES_CONTAINER_NODE_VERSION" ]; then
        echo "ERROR: expected immutable Node $HERMES_CONTAINER_NODE_VERSION, got $actual_version" >&2
        return 1
    fi

    # The immutable Node always wins over a stale apt Node in the persistent
    # writable layer. npm global installs remain writable and come immediately
    # after Node itself.
    npm_global_bin="${TARGET_HOME:-${HOME:-/home/hermes}}/.npm-global/bin"
    mkdir -p "$npm_global_bin"
    if [ -n "${HERMES_UID:-}" ] && [ -n "${HERMES_GID:-}" ]; then
        chown -R "$HERMES_UID:$HERMES_GID" "${npm_global_bin%/bin}"
    fi
    PATH="$HERMES_CONTAINER_NODE_BIN:$npm_global_bin:$PATH"
    NPM_CONFIG_PREFIX=${npm_global_bin%/bin}
    export PATH NPM_CONFIG_PREFIX

    selected=$(command -v node 2>/dev/null || true)
    if [ "$selected" != "$node_bin" ]; then
        echo "ERROR: immutable Hermes Node did not win PATH (selected $selected)" >&2
        return 1
    fi
}
