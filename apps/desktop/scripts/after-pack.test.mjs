import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { test } from 'vitest'
import afterPack from './after-pack.mjs'

test('explicit external signing hook runs on macOS and failures propagate', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'hermes-sign-hook-'))
  const previous = process.env.HERMES_DESKTOP_AFTERPACK_HOOK
  try {
    const good = path.join(root, 'good hook.mjs')
    await fs.writeFile(good, 'export default async context => { context.signed = true }')
    process.env.HERMES_DESKTOP_AFTERPACK_HOOK = good
    const context = { electronPlatformName: 'darwin' }
    await afterPack(context)
    assert.equal(context.signed, true)
    const bad = path.join(root, 'bad.mjs')
    await fs.writeFile(bad, 'export default async () => { throw new Error("signing rejected") }')
    process.env.HERMES_DESKTOP_AFTERPACK_HOOK = bad
    await assert.rejects(afterPack(context), /signing rejected/)
    delete process.env.HERMES_DESKTOP_AFTERPACK_HOOK
    await afterPack({ electronPlatformName: 'darwin' })
  } finally {
    if (previous === undefined) delete process.env.HERMES_DESKTOP_AFTERPACK_HOOK
    else process.env.HERMES_DESKTOP_AFTERPACK_HOOK = previous
    await fs.rm(root, { recursive: true, force: true })
  }
})
