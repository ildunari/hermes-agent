/**
 * after-pack.cjs — electron-builder afterPack hook.
 *
 * Stamps the Hermes icon + identity onto the packed Windows Hermes.exe via
 * rcedit (delegated to set-exe-identity.cjs). This runs for EVERY packed build
 * — first install, `hermes desktop`, the installer's --update rebuild, and a
 * dev's manual `npm run pack` — so the branded exe can never silently revert
 * to the stock "Electron" icon/name (the bug when the stamp lived only in
 * install.ps1, which the update path doesn't use).
 *
 * Windows-only: rcedit edits PE resources, irrelevant on macOS/Linux where the
 * app identity comes from the bundle Info.plist / desktop entry. Best-effort:
 * a stamp failure must never fail an otherwise-good build (worst case is the
 * stock icon, not a broken app), so we log and resolve rather than throw.
 *
 * electron-builder passes a context with:
 *   - electronPlatformName: 'win32' | 'darwin' | 'linux'
 *   - appOutDir:            the unpacked app directory for this target
 *   - packager.appInfo.productFilename: the exe basename (e.g. 'Hermes')
 */

const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { execFileSync, spawnSync } = require('node:child_process')

const { stampExeIdentity } = require('./set-exe-identity.cjs')

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

  const op = spawnSync('op', ['item', 'get', HERMES_DEVELOPER_ID_KEYCHAIN_ITEM, '--vault', 'CLI', '--reveal', '--fields', 'password'], {
    encoding: 'utf8',
  })
  if (op.status !== 0) return
  const password = op.stdout.trim()
  if (!password) return

  for (const keychain of keychains) {
    spawnSync('/usr/bin/security', ['unlock-keychain', '-p', password, keychain], { stdio: 'ignore' })
  }
}

function canCodesignWithIdentity(identity) {
  if (process.platform !== 'darwin') return false
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-codesign-probe-'))
  const probe = path.join(tmpDir, 'probe')
  try {
    fs.writeFileSync(probe, 'probe\n')
    fs.chmodSync(probe, 0o755)
    const result = spawnSync('/usr/bin/codesign', ['--force', '--sign', identity, '--timestamp=none', probe], {
      encoding: 'utf8',
    })
    return result.status === 0
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true })
  }
}

function preferredMacSigningIdentity() {
  if (process.env.CSC_NAME || process.env.CSC_LINK) return null
  unlockHermesSigningKeychains()
  if (canCodesignWithIdentity(HERMES_DEVELOPER_ID_SIGNING_IDENTITY)) return HERMES_DEVELOPER_ID_SIGNING_IDENTITY
  if (canCodesignWithIdentity(HERMES_LOCAL_SIGNING_IDENTITY)) return HERMES_LOCAL_SIGNING_IDENTITY
  return null
}

function localSignMacApp(context) {
  const signingIdentity = preferredMacSigningIdentity()
  if (!signingIdentity) {
    return
  }

  const productName = context.packager?.appInfo?.productFilename || 'Hermes'
  const appPath = path.join(context.appOutDir, `${productName}.app`)
  if (!fs.existsSync(appPath)) return

  const desktopRoot = path.resolve(__dirname, '..')
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
    { stdio: 'inherit' }
  )
  console.log(`[after-pack] signed ${appPath} with ${signingIdentity}`)
}

exports.default = async function afterPack(context) {
  if (context.electronPlatformName === 'darwin') {
    localSignMacApp(context)
    return
  }

  if (context.electronPlatformName !== 'win32') {
    return
  }

  const productName = context.packager?.appInfo?.productFilename || 'Hermes'
  const exe = path.join(context.appOutDir, `${productName}.exe`)
  const desktopRoot = path.resolve(__dirname, '..')

  try {
    await stampExeIdentity(exe, desktopRoot)
  } catch (err) {
    // Never fail the build over a cosmetic stamp.
    console.warn(`[after-pack] exe identity stamp failed (${err.message}); Hermes.exe keeps the stock Electron icon`)
  }
}
