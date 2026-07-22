import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'

import { afterEach, describe, expect, it } from 'vitest'

import { BrowserActivityRepository, browserProfileScope } from './browser-activity-repository'

const roots: string[] = []

const base = (overrides = {}) => ({
  bindingGeneration: 3,
  category: 'pause',
  certainty: 'completed',
  phase: 'completed',
  reasonCode: 'retired',
  source: 'human',
  tabIncarnationId: 'browser:opaque',
  workspaceId: 'workspace:opaque',
  ...overrides
})

function repository(profile = 'default', now: () => number = () => 10_000_000_000) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-browser-activity-'))
  roots.push(root)

  return new BrowserActivityRepository(root, profile, now)
}

afterEach(() => {
  for (const root of roots.splice(0)) {fs.rmSync(root, { force: true, recursive: true })}
})

describe('BrowserActivityRepository', () => {
  it('uses the canonical isolated profile scope and real SQLite repository', () => {
    expect(browserProfileScope('')).toBe('O7U3sPz8CoQw576B7YjZjO')
    expect(browserProfileScope(' Coding ')).toBe('TikzYIaYz8WsHCfGsGIDLa')
    const repo = repository('coding')
    expect(repo.open()).toBe(true)
    expect(repo.file).toContain('/browser/v1/TikzYIaYz8WsHCfGsGIDLa/state.sqlite3')
    const db = new DatabaseSync(repo.file, { readOnly: true })
    expect(db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='browser_action_event'").get()).toBeTruthy()
    db.close()
    repo.close()
    expect(() => browserProfileScope('../escape')).toThrow('invalid browser profile')
  })

  it('writes only bounded allowlisted projections and rejects private or unsafe data', () => {
    const repo = repository()
    expect(repo.append(base({ siteIdentity: 'https://example.com/' }))).toMatchObject({ ok: true })
    expect(repo.append(base({ private: true }))).toEqual({ error: 'invalid', ok: false })
    expect(repo.append(base({ siteIdentity: 'https://example.com/secret?token=canary' }))).toMatchObject({ ok: false })
    expect(repo.list()).toHaveLength(1)
    const serialized = JSON.stringify(repo.list())
    expect(serialized).not.toContain('secret')
    expect(serialized).not.toContain('canary')
    repo.close()
  })

  it('settles inherited nonterminal phases conservatively without changing terminal rows', () => {
    const repo = repository()

    for (const [index, phase] of ['proposed', 'awaiting_consent', 'admitted', 'dispatched'].entries()) {
      expect(repo.append(base({ certainty: null, eventId: `event-${index}`, phase, reasonCode: null, terminalAt: null }))).toMatchObject({ ok: true })
    }

    expect(repo.append(base({ eventId: 'terminal' }))).toMatchObject({ ok: true })
    repo.close()

    const reopened = new BrowserActivityRepository(path.dirname(path.dirname(path.dirname(path.dirname(repo.file)))), 'default', () => 10_000_000_100)
    expect(reopened.open()).toBe(true)
    const rows = reopened.list(20)
    expect(rows.filter(row => row.eventId !== 'terminal').map(row => [row.phase, row.certainty, row.reasonCode])).toEqual([
      ['not_started', 'not_started', 'app_restart'],
      ['not_started', 'not_started', 'app_restart'],
      ['not_started', 'not_started', 'app_restart'],
      ['outcome_unknown', 'outcome_unknown', 'app_restart']
    ])
    expect(rows.find(row => row.eventId === 'terminal')).toMatchObject({ phase: 'completed', reasonCode: 'retired' })
    reopened.close()
  })

  it('prunes by seven-day age and exact-session deletion without touching unrelated rows', () => {
    let now = 8 * 24 * 60 * 60 * 1_000
    const repo = repository('default', () => now)
    expect(repo.append(base({ createdAt: 0, eventId: 'expired', sessionId: 'session-a' }))).toMatchObject({ ok: true })
    expect(repo.list()).toHaveLength(0)
    expect(repo.append(base({ eventId: 'a', sessionId: 'session-a' }))).toMatchObject({ ok: true })
    expect(repo.append(base({ eventId: 'b', sessionId: 'session-b' }))).toMatchObject({ ok: true })
    expect(repo.deleteSession('session-a')).toBe(true)
    expect(repo.list().map(row => row.eventId)).toEqual(['b'])
    repo.close()
  })

  it('enforces the hard count cap by pruning terminal rows before nonterminal rows', () => {
    const repo = repository()
    expect(repo.open()).toBe(true)
    const db = new DatabaseSync(repo.file)

    const insert = db.prepare(`INSERT INTO browser_action_event (
      event_id, workspace_id, sequence, tab_incarnation_id, binding_generation, source, category, phase,
      created_at, checkpoint_attempted, checkpoint_available_at_capture, private
    ) VALUES (?, 'workspace', ?, 'tab', 1, 'system', 'action', ?, ?, 0, 0, 0)`)

    db.exec('BEGIN IMMEDIATE')
    insert.run('pending', 1, 'dispatched', 10_000_000_001)

    for (let sequence = 2; sequence <= 5001; sequence += 1) {
      insert.run(`terminal-${sequence}`, sequence, 'completed', 10_000_000_000 + sequence)
    }

    db.exec('COMMIT')
    expect(repo.prune()).toBe(true)
    expect(db.prepare('SELECT COUNT(*) AS count FROM browser_action_event').get()?.count).toBe(5000)
    expect(db.prepare("SELECT phase FROM browser_action_event WHERE event_id='pending'").get()).toEqual({ phase: 'dispatched' })
    expect(db.prepare("SELECT event_id FROM browser_action_event WHERE event_id='terminal-2'").get()).toBeUndefined()
    db.close()
    repo.close()
  })

  it('fails closed for an unknown future schema rather than recreating the database', () => {
    const repo = repository()
    fs.mkdirSync(path.dirname(repo.file), { recursive: true })
    const db = new DatabaseSync(repo.file)
    db.exec('PRAGMA user_version=99')
    db.close()
    expect(repo.open()).toBe(false)
    expect(repo.degraded).toBe(true)
    expect(fs.existsSync(repo.file)).toBe(true)
  })
})
