import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { test } from 'vitest'

import { buildConnectionBootstrapScript } from './browser-dev-connection.mjs'

function fixture() {
  const root = mkdtempSync(join(tmpdir(), 'hermes-browser-dev-connection-'))
  const userData = join(root, 'Hermes Browser Dev')

  mkdirSync(userData, { recursive: true })

  return { userData }
}

function runBootstrap(options) {
  execFileSync('/bin/bash', ['-c', buildConnectionBootstrapScript(options)])
}

test('points isolated Browser Dev state at a real Hermes profile over SSH', () => {
  const { userData } = fixture()

  runBootstrap({
    activeProfile: 'coding',
    backend: {
      host: 'macstudio.example',
      keyPath: '/Users/kosta/.ssh/studio',
      port: 2222,
      remoteHermesPath: '/Users/Kosta/.local/bin/hermes',
      user: 'Kosta'
    },
    userData
  })

  assert.deepEqual(JSON.parse(readFileSync(join(userData, 'connection.json'), 'utf8')), {
    mode: 'ssh',
    profiles: {},
    remote: {
      host: 'macstudio.example',
      keyPath: '/Users/kosta/.ssh/studio',
      mode: 'ssh',
      port: 2222,
      remoteHermesPath: '/Users/Kosta/.local/bin/hermes',
      user: 'Kosta'
    }
  })
  assert.deepEqual(JSON.parse(readFileSync(join(userData, 'active-profile.json'), 'utf8')), {
    profile: 'coding'
  })
  assert.equal(statSync(join(userData, 'connection.json')).mode & 0o777, 0o600)
  assert.equal(statSync(join(userData, 'active-profile.json')).mode & 0o777, 0o600)
})

test('falls back to a clean local connection when no Browser Dev backend host is configured', () => {
  const { userData } = fixture()

  writeFileSync(join(userData, 'connection.json'), '{"mode":"remote"}')
  writeFileSync(join(userData, 'active-profile.json'), '{"profile":"stale"}')

  runBootstrap({ backend: {}, userData })

  assert.deepEqual(JSON.parse(readFileSync(join(userData, 'connection.json'), 'utf8')), {
    mode: 'local',
    profiles: {}
  })
  assert.throws(() => readFileSync(join(userData, 'active-profile.json'), 'utf8'), { code: 'ENOENT' })
})
