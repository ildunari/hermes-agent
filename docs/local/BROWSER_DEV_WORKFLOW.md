# Hermes Browser Dev: Studio → MacBook live preview

This is the fast iteration path for the Electron browser side panel. Code and Vite run on the Mac Studio. An isolated Electron shell named **Hermes Browser Dev** runs on the MacBook Pro and reaches Vite through a localhost SSH tunnel carried over Tailscale. Keeping the renderer URL on `127.0.0.1` preserves browser secure-context APIs while avoiding a LAN-exposed development port. The installed Hermes app remains open for the conversation.

## What is isolated

Browser Dev uses its own Electron data directory:

```text
~/Library/Application Support/Hermes Browser Dev
```

It has a separate process, browser session/cookies, window state, local backend process, log, PID file, and CDP port (`9231`). The coordinator pins this disposable shell to local mode, using the MacBook's Hermes checkout for backend APIs; it does not copy credentials or share the installed app's mutable Electron data directory.

The staged MacBook source lives under:

```text
~/.cache/hermes-browser-dev/hermes-agent
```

That directory is disposable. The Studio worktree remains the source of truth.

## Start

From a separate Studio worktree based on `local/studio-slim`:

```bash
cd apps/desktop
npm run dev:browser
```

The command does all of the following:

1. Verifies the MacBook is reachable through its Tailscale SSH hostname.
2. Starts Vite on Studio localhost at port `5174`.
3. Opens a reverse SSH tunnel over Tailscale so MacBook localhost `5174` reaches Studio Vite.
4. Stages the Desktop Electron source onto the MacBook over SSH/rsync.
5. Installs or refreshes the disposable remote dependencies only when `package-lock.json` changes.
6. Builds the Electron main/preload development bundle on the MacBook.
7. Launches **Hermes Browser Dev** with isolated app data and opens the browser pane automatically.
8. Watches `apps/desktop/electron/**` and the Electron bundler for deeper changes.

Keep this command running while iterating. Continue chatting through the regular installed Hermes window; use Browser Dev only as the live preview and test surface.

## Reload behavior

- Renderer changes under `apps/desktop/src/**` and CSS changes use Vite HMR directly from the Studio. They normally appear without restarting Browser Dev.
- Electron main, preload, browser security, IPC, and guest-lifecycle changes are rsynced to the MacBook, rebundled there, and relaunch only Browser Dev.
- The installed Hermes app and the regular chat are not restarted by either path.

## Status and stop

In another Studio terminal:

```bash
cd apps/desktop
npm run dev:browser:status
npm run dev:browser:stop
```

Normally, press `Ctrl-C` in the terminal running `npm run dev:browser`; the coordinator stops Vite and the remote Browser Dev process together. The explicit stop command is for a detached/stale preview.

## Logs and clean reset

Read the remote development log from the Studio:

```bash
ssh-macbook 'tail -n 200 "$HOME/Library/Application Support/Hermes Browser Dev/browser-dev.log"'
```

Reset only the disposable Browser Dev state:

```bash
npm run dev:browser:stop
ssh-macbook 'rm -rf "$HOME/Library/Application Support/Hermes Browser Dev" "$HOME/.cache/hermes-browser-dev"'
```

Do not delete the regular `~/Library/Application Support/Hermes` directory.

## Overrides

The defaults match Kosta's Studio/MacBook Tailscale setup. These environment variables make the coordinator portable or avoid a port collision:

```text
HERMES_BROWSER_DEV_PORT
HERMES_BROWSER_DEV_CDP_PORT
HERMES_BROWSER_DEV_SSH_TARGET
HERMES_BROWSER_DEV_SSH_KEY
HERMES_BROWSER_DEV_REMOTE_ROOT
HERMES_BROWSER_DEV_REMOTE_MODULES
HERMES_BROWSER_DEV_USER_DATA
```

Example:

```bash
HERMES_BROWSER_DEV_PORT=5175 npm run dev:browser
```

The MacBook must be reachable over Tailscale and have Node/npm available. The coordinator maintains its own disposable dependency tree under `~/.cache/hermes-browser-dev`, refreshing it when the repository lockfile changes. It fails loudly if SSH, rsync, Vite, the tunnel, the remote bundle, or the Electron launch fails.
