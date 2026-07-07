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

const HERMES_LOCAL_SIGNING_IDENTITY = 'Hermes Desktop Local Signing'

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

function localSignMacApp(context) {
  if (process.env.CSC_NAME || process.env.CSC_LINK || !canCodesignWithIdentity(HERMES_LOCAL_SIGNING_IDENTITY)) {
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
      HERMES_LOCAL_SIGNING_IDENTITY,
      appPath,
    ],
    { stdio: 'inherit' }
  )
  console.log(`[after-pack] signed ${appPath} with ${HERMES_LOCAL_SIGNING_IDENTITY}`)
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
