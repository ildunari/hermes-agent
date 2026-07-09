/**
 * after-pack.mjs — electron-builder afterPack hook.
 *
 * Local macOS packs are signed deterministically after electron-builder stages
 * the bundle. Windows packs keep their branded executable metadata via rcedit.
 */

import { execFileSync, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { stampExeIdentity } from './set-exe-identity.mjs'

const HERMES_DEVELOPER_ID_SIGNING_IDENTITY = '3A22F53A48A189F4A8766CACE00192860CC37F8F'
const HERMES_DEVELOPER_ID_KEYCHAIN_ITEM = 'Hermes Developer ID Signing Keychain'
const HERMES_LOCAL_SIGNING_IDENTITY = 'Hermes Desktop Local Signing'

let didTryUnlockSigningKeychains = false

function unlockHermesSigningKeychains() {
  if (didTryUnlockSigningKeychains || process.platform !== 'darwin') return
  didTryUnlockSigningKeychains = true

  const keychains = [
    path.join(os.homedir(), 'Library', 'Keychains', 'hermes-developer-id-signing.keychain-db'),
    path.join(os.homedir(), 'Library', 'Keychains', 'hermes-desktop-signing.keychain-db'),
  ].filter(fs.existsSync)
  if (keychains.length === 0) return

  const op = spawnSync(
    'op',
    ['item', 'get', HERMES_DEVELOPER_ID_KEYCHAIN_ITEM, '--vault', 'CLI', '--reveal', '--fields', 'password'],
    { encoding: 'utf8' },
  )
  if (op.status !== 0) return
  const password = op.stdout.trim()
  if (!password) return

  for (const keychain of keychains) {
    spawnSync('/usr/bin/security', ['unlock-keychain', '-p', password, keychain], { stdio: 'ignore' })
    // Grant non-interactive access to Apple signing tools. Without this ACL,
    // codesign can still raise a Keychain approval dialog despite a successful
    // CLI unlock, which breaks unattended smart updates.
    spawnSync(
      '/usr/bin/security',
      ['set-key-partition-list', '-S', 'apple-tool:,apple:,codesign:', '-s', '-k', password, keychain],
      { stdio: 'ignore' },
    )
  }
}

function canCodesignWithIdentity(identity) {
  if (process.platform !== 'darwin') return false
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-codesign-probe-'))
  const probe = path.join(tmpDir, 'probe')
  try {
    fs.writeFileSync(probe, 'probe\n')
    fs.chmodSync(probe, 0o755)
    const result = spawnSync(
      '/usr/bin/codesign',
      ['--force', '--sign', identity, '--timestamp=none', probe],
      { encoding: 'utf8' },
    )
    return result.status === 0
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true })
  }
}

function preferredMacSigningIdentity() {
  if (process.env.CSC_NAME || process.env.CSC_LINK) return null
  unlockHermesSigningKeychains()
  if (canCodesignWithIdentity(HERMES_DEVELOPER_ID_SIGNING_IDENTITY)) {
    return HERMES_DEVELOPER_ID_SIGNING_IDENTITY
  }
  if (canCodesignWithIdentity(HERMES_LOCAL_SIGNING_IDENTITY)) {
    return HERMES_LOCAL_SIGNING_IDENTITY
  }
  return null
}

function localSignMacApp(context) {
  const signingIdentity = preferredMacSigningIdentity()
  if (!signingIdentity) return

  const productName = context.packager?.appInfo?.productFilename || 'Hermes'
  const appPath = path.join(context.appOutDir, `${productName}.app`)
  if (!fs.existsSync(appPath)) return

  const desktopRoot = path.resolve(import.meta.dirname, '..')
  const entitlements = path.join(desktopRoot, 'electron', 'entitlements.mac.plist')
  execFileSync(
    '/usr/bin/codesign',
    [
      '--force',
      '--deep',
      '--timestamp=none',
      '--options',
      'runtime',
      '--entitlements',
      entitlements,
      '--sign',
      signingIdentity,
      appPath,
    ],
    { stdio: 'inherit' },
  )
  console.log(`[after-pack] signed ${appPath} with ${signingIdentity}`)
}

export default async function afterPack(context) {
  if (context.electronPlatformName === 'darwin') {
    localSignMacApp(context)
    return
  }

  if (context.electronPlatformName !== 'win32') return

  const productName = context.packager?.appInfo?.productFilename || 'Hermes'
  const exe = path.join(context.appOutDir, `${productName}.exe`)
  const desktopRoot = path.resolve(import.meta.dirname, '..')

  try {
    await stampExeIdentity(exe, desktopRoot)
  } catch (err) {
    // Never fail the build over a cosmetic stamp.
    console.warn(`[after-pack] exe identity stamp failed (${err.message}); Hermes.exe keeps the stock Electron icon`)
  }
}
