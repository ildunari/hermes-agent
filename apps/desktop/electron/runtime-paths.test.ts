import assert from 'node:assert/strict'
import path from 'node:path'
import { describe, it } from 'node:test'

import { resolveRepoVenvRoot } from './runtime-paths'

describe('resolveRepoVenvRoot', () => {
  it('prefers .venv when both environments exist', () => {
    const root = path.join('/tmp', 'hermes-agent')
    const existing = new Set([path.join(root, '.venv'), path.join(root, 'venv')])

    assert.equal(resolveRepoVenvRoot(root, candidate => existing.has(candidate)), path.join(root, '.venv'))
  })

  it('falls back to legacy venv when it is the only environment', () => {
    const root = path.join('/tmp', 'hermes-agent')
    const legacy = path.join(root, 'venv')

    assert.equal(resolveRepoVenvRoot(root, candidate => candidate === legacy), legacy)
  })

  it('returns the canonical .venv target before bootstrap creates it', () => {
    const root = path.join('/tmp', 'hermes-agent')

    assert.equal(resolveRepoVenvRoot(root, () => false), path.join(root, '.venv'))
  })
})
