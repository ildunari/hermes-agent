import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { execFileSync, spawnSync } from 'node:child_process'

if (process.platform !== 'darwin') process.exit(0)

const desktopRoot = path.resolve(import.meta.dirname, '..')
const appPath = path.join(desktopRoot, 'release', 'mac-arm64', 'Hermes.app')
const entitlements = path.join(desktopRoot, 'build', 'entitlements.mac.plist')
const keychain = path.join(os.homedir(), 'Library', 'Keychains', 'hermes-developer-id-signing.keychain-db')
const identity = '3A22F53A48A189F4A8766CACE00192860CC37F8F'

if (!fs.existsSync(appPath)) {
  console.error(`[sign-packed-mac] app not found: ${appPath}`)
  process.exit(1)
}

if (fs.existsSync(keychain)) {
  const op = spawnSync('op', [
    'item',
    'get',
    'Hermes Developer ID Signing Keychain',
    '--vault',
    'CLI',
    '--reveal',
    '--fields',
    'password',
  ], { encoding: 'utf8' })
  const password = op.stdout.trim()
  if (op.status !== 0 || !password) {
    console.error('[sign-packed-mac] could not read signing-keychain password from 1Password')
    process.exit(1)
  }
  const unlock = spawnSync('/usr/bin/security', ['unlock-keychain', '-p', password, keychain], { stdio: 'inherit' })
  if (unlock.status !== 0) process.exit(unlock.status ?? 1)
}

execFileSync('/usr/bin/codesign', [
  '--force',
  '--deep',
  '--options',
  'runtime',
  '--entitlements',
  entitlements,
  '--sign',
  identity,
  appPath,
], { stdio: 'inherit' })

execFileSync('/usr/bin/codesign', ['--verify', '--deep', '--strict', '--verbose=2', appPath], { stdio: 'inherit' })
console.log(`[sign-packed-mac] signed ${appPath} with Developer ID ${identity}`)
