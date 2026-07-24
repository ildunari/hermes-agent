import { join } from 'node:path'

export function shellQuote(value) {
  return `'${String(value).replaceAll("'", `'\\''`)}'`
}

/**
 * Build the remote shell fragment that points Browser Dev at a real Hermes host
 * through Desktop's existing SSH connection mode. The dev shell mints and
 * encrypts its own dashboard token; it never copies the installed app's token or
 * shares its mutable user-data tree.
 */
export function buildConnectionBootstrapScript({ backend, activeProfile = '', userData }) {
  if (!userData) {
    throw new Error('Browser Dev connection bootstrap requires a destination user-data path')
  }

  const destinationConnection = join(userData, 'connection.json')
  const destinationProfile = join(userData, 'active-profile.json')
  const host = String(backend?.host || '').trim()
  const user = String(backend?.user || '').trim()
  const keyPath = String(backend?.keyPath || '').trim()
  const remoteHermesPath = String(backend?.remoteHermesPath || '').trim()
  const port = Number(backend?.port)
  const remote = { mode: 'ssh', host }

  if (user) remote.user = user
  if (Number.isInteger(port) && port > 0 && port <= 65535 && port !== 22) remote.port = port
  if (keyPath) remote.keyPath = keyPath
  if (remoteHermesPath) remote.remoteHermesPath = remoteHermesPath

  const connection = host ? { mode: 'ssh', profiles: {}, remote } : { mode: 'local', profiles: {} }
  const profile = String(activeProfile || '').trim()

  return `
DEST_CONNECTION=${shellQuote(destinationConnection)}
DEST_PROFILE=${shellQuote(destinationProfile)}
printf '%s\n' ${shellQuote(JSON.stringify(connection))} > "$DEST_CONNECTION.tmp"
chmod 600 "$DEST_CONNECTION.tmp"
mv "$DEST_CONNECTION.tmp" "$DEST_CONNECTION"
${
  profile
    ? `printf '%s\n' ${shellQuote(JSON.stringify({ profile }))} > "$DEST_PROFILE.tmp"
chmod 600 "$DEST_PROFILE.tmp"
mv "$DEST_PROFILE.tmp" "$DEST_PROFILE"`
    : 'rm -f "$DEST_PROFILE" "$DEST_PROFILE.tmp"'
}
`
}
