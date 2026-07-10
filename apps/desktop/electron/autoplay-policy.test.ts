import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const ELECTRON_DIR = path.dirname(fileURLToPath(import.meta.url))

test('desktop permits asynchronous read-aloud playback after its user gesture expires', () => {
  const source = fs.readFileSync(path.join(ELECTRON_DIR, 'main.ts'), 'utf8')

  assert.match(
    source,
    /app\.commandLine\.appendSwitch\('autoplay-policy', 'no-user-gesture-required'\)/
  )
})
