import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'

import { afterEach, describe, expect, it } from 'vitest'

import { BrowserActivityRepository, BrowserActivityRepositoryManager } from './browser-activity-repository'
import { repairBrowserProfileMetadata } from './browser-state-repair'
import {
  BrowserStateRepository,
  BrowserStateRepositoryManager,
  sanitizeBrowserPersistenceUrl
} from './browser-state-repository'

const roots: string[] = []

function repository(profile = 'default', now: () => number = () => 10_000_000_000) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-browser-state-'))
  roots.push(root)
  return { repo: new BrowserStateRepository(root, profile, now), root }
}

function descriptor(overrides = {}) {
  return {
    ordinal: 0,
    restoreId: 'restore-one',
    restoredFromTabId: 'browser:old-incarnation',
    title: 'Example',
    url: 'https://example.test/path?q=ordinary#volatile',
    workspaceId: 'workspace-one',
    ...overrides
  }
}

afterEach(() => {
  for (const root of roots.splice(0)) {fs.rmSync(root, { force: true, recursive: true })}
})

describe('BrowserStateRepository', () => {
  it('persists only sanitized ordinary restore intent and bounded local history', () => {
    const { repo } = repository()
    const initial = repo.snapshot()

    expect(initial).toMatchObject({ degraded: false, descriptors: [], selectedRestoreId: null })
    expect(repo.upsert(descriptor(), initial.epoch)).toMatchObject({ ok: true })
    expect(repo.select('workspace-one', 'restore-one', initial.epoch)).toBe(true)

    const snapshot = repo.snapshot()
    expect(snapshot.selectedRestoreId).toBe('restore-one')
    expect(snapshot.descriptors).toEqual([
      expect.objectContaining({
        restoreId: 'restore-one',
        restoredFromTabId: 'browser:old-incarnation',
        url: 'https://example.test/path?q=ordinary',
        workspaceId: 'workspace-one'
      })
    ])
    expect(repo.history()).toEqual([
      expect.objectContaining({
        origin: 'https://example.test',
        redactionClass: 'none',
        url: 'https://example.test/path?q=ordinary'
      })
    ])
    repo.close()
  })

  it('rejects private/internal/credential-bearing restore material and strips fragments', () => {
    const { repo } = repository()
    const { epoch } = repo.snapshot()

    for (const url of [
      'file:///tmp/secret',
      'hermes-artifact://g-secret/report',
      'https://user:pass@example.test/',
      'https://example.test/callback?access_token=canary'
    ]) {
      expect(repo.upsert(descriptor({ restoreId: `restore-${url.length}`, url }), epoch)).toEqual({ ok: false })
    }

    expect(repo.snapshot().descriptors).toEqual([])
    expect(repo.history()).toEqual([])
    expect(sanitizeBrowserPersistenceUrl('https://example.test/a#secret').restoreUrl).toBe('https://example.test/a')
    expect(sanitizeBrowserPersistenceUrl('https://example.test/a?token=secret')).toMatchObject({
      redactionClass: 'sensitive',
      restoreUrl: null,
      visitUrl: 'https://example.test'
    })
    repo.close()
  })

  it('rotates mutation authority on workspace reset and keeps site data outside metadata scope', () => {
    const { repo, root } = repository()
    const { epoch } = repo.snapshot()
    expect(repo.upsert(descriptor(), epoch).ok).toBe(true)

    const reset = repo.resetWorkspace('workspace-one', false)
    expect(reset.ok).toBe(true)
    expect(reset.epoch).not.toBe(epoch)
    expect(repo.snapshot().descriptors).toEqual([])
    expect(repo.history()).toHaveLength(1)
    expect(repo.upsert(descriptor({ url: 'https://stale.test/' }), epoch)).toEqual({ ok: false })
    expect(repo.upsert(descriptor({ url: 'https://fresh.test/' }), reset.epoch).ok).toBe(true)

    const db = new DatabaseSync(repo.file, { readOnly: true })
    expect(db.prepare('PRAGMA user_version').get()?.user_version).toBe(3)
    expect(JSON.stringify(db.prepare('SELECT * FROM tab_restore').all())).not.toContain('stale.test')
    db.close()
    repo.close()
    expect(fs.existsSync(path.join(root, 'browser'))).toBe(true)
  })

  it('bounds history by age and row count without persisting duplicate URL writes', () => {
    let now = 40 * 24 * 60 * 60 * 1_000
    const { repo } = repository('default', () => now)
    const { epoch } = repo.snapshot()

    expect(repo.upsert(descriptor({ updatedAt: 0 }), epoch).ok).toBe(true)
    expect(repo.history()).toEqual([])

    const db = new DatabaseSync(repo.file)
    const insert = db.prepare(`INSERT INTO visit_history (
      visit_id, workspace_id, url, origin, title, redaction_class, visited_at
    ) VALUES (?, 'workspace-one', ?, 'https://example.test', '', 'none', ?)`)
    db.exec('BEGIN IMMEDIATE')
    for (let index = 0; index < 5_001; index += 1) {
      insert.run(`visit-${index}`, `https://example.test/${index}`, now + index)
    }
    db.exec('COMMIT')
    db.close()

    expect(repo.prune()).toBe(true)
    const verify = new DatabaseSync(repo.file, { readOnly: true })
    expect(verify.prepare('SELECT COUNT(*) AS count FROM visit_history').get()?.count).toBe(5_000)
    expect(verify.prepare("SELECT 1 FROM visit_history WHERE visit_id='visit-0'").get()).toBeUndefined()
    verify.close()
    repo.close()
  })

  it('shares the versioned profile database with bounded activity without clobbering either table set', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-browser-shared-db-'))
    roots.push(root)
    const activity = new BrowserActivityRepository(root, 'coding', () => 10_000_000_000)
    const state = new BrowserStateRepository(root, 'coding', () => 10_000_000_000)
    expect(activity.open()).toBe(true)
    const epoch = state.snapshot().epoch
    expect(state.upsert(descriptor(), epoch).ok).toBe(true)
    expect(activity.append({
      bindingGeneration: 1,
      category: 'navigate',
      certainty: 'completed',
      phase: 'completed',
      source: 'agent',
      tabIncarnationId: 'browser:live',
      workspaceId: 'workspace-one'
    }).ok).toBe(true)
    expect(state.snapshot().descriptors).toHaveLength(1)
    expect(activity.list()).toHaveLength(1)
    state.close()
    activity.close()
  })

  it('isolates restore and history rows across profile databases', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-browser-state-isolation-'))
    roots.push(root)
    const manager = new BrowserStateRepositoryManager(root)
    const coding = manager.forProfile('coding')
    const research = manager.forProfile('research')
    const codingEpoch = coding.snapshot().epoch
    const researchEpoch = research.snapshot().epoch

    expect(coding.upsert(descriptor({ restoreId: 'coding-restore', url: 'https://coding.test/' }), codingEpoch).ok).toBe(true)
    expect(research.upsert(descriptor({ restoreId: 'research-restore', url: 'https://research.test/' }), researchEpoch).ok).toBe(true)
    expect(coding.snapshot().descriptors.map(row => row.restoreId)).toEqual(['coding-restore'])
    expect(research.snapshot().descriptors.map(row => row.restoreId)).toEqual(['research-restore'])
    expect(coding.file).not.toBe(research.file)
    manager.close()
  })

  it('migrates schema v2 and keeps exact-origin durable permissions profile-local', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-browser-state-permissions-'))
    roots.push(root)
    const manager = new BrowserStateRepositoryManager(root)
    const coding = manager.forProfile('coding')
    expect(coding.open()).toBe(true)
    coding.close()
    const old = new DatabaseSync(coding.file)
    old.exec('DROP TABLE permission_decision; DROP TABLE transfer_provenance; DROP TABLE browser_preference; PRAGMA user_version=2')
    old.close()

    expect(coding.open()).toBe(true)
    expect(coding.setPermission('https://example.test', 'notifications', 'allow')).toBe(true)
    expect(coding.permission('https://example.test', 'notifications')).toMatchObject({ decision: 'allow' })
    expect(coding.permission('https://sub.example.test', 'notifications')).toBeNull()
    expect(coding.setPermission('https://example.test/path', 'notifications', 'allow')).toBe(false)
    expect(coding.setPermission('https://example.test', 'notifications', 'deny', 'session')).toBe(false)
    expect(manager.forProfile('research').permission('https://example.test', 'notifications')).toBeNull()
    manager.close()
  })

  it('stores only redacted human transfer provenance and clears ledgers explicitly', () => {
    const { repo } = repository()
    expect(repo.appendTransfer({
      actor: 'human', byteSize: 42, digest: `sha256:${'a'.repeat(64)}`, direction: 'download',
      origin: 'https://example.test', outcome: 'completed', redactedName: 'report.pdf', tabIncarnationId: 'browser:live'
    }).ok).toBe(true)
    expect(repo.appendTransfer({
      actor: 'human', direction: 'upload', origin: 'https://example.test', outcome: 'completed', private: true,
      redactedName: 'private.txt', tabIncarnationId: 'browser:private'
    }).ok).toBe(false)
    expect(repo.appendTransfer({
      actor: 'human', direction: 'upload', origin: 'https://example.test', outcome: 'completed',
      redactedName: '/Users/kosta/secret.txt', tabIncarnationId: 'browser:live'
    }).ok).toBe(false)
    expect(repo.transfers()).toEqual([expect.objectContaining({ redactedName: 'report.pdf', origin: 'https://example.test' })])
    expect(JSON.stringify(repo.transfers())).not.toContain('/Users/')
    expect(repo.clearBrowsingMetadata({ transfers: true })).toBe(true)
    expect(repo.transfers()).toEqual([])
    repo.close()
  })

  it('disables restore without deleting history and re-enables future writes', () => {
    const { repo } = repository()
    const initial = repo.snapshot()
    expect(repo.upsert(descriptor(), initial.epoch).ok).toBe(true)
    expect(repo.setRestoreEnabled(false)).toBe(true)
    expect(repo.restoreEnabled()).toBe(false)
    expect(repo.snapshot().descriptors).toEqual([])
    expect(repo.history()).toHaveLength(1)
    expect(repo.upsert(descriptor({ restoreId: 'disabled' }), repo.snapshot().epoch).ok).toBe(false)
    expect(repo.setRestoreEnabled(true)).toBe(true)
    expect(repo.upsert(descriptor({ restoreId: 'enabled' }), repo.snapshot().epoch).ok).toBe(true)
    repo.close()
  })

  it('tombstones profile deletion, removes metadata, and recreates the same name empty', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-browser-state-manager-'))
    roots.push(root)
    const manager = new BrowserStateRepositoryManager(root)
    const repo = manager.forProfile('coding')
    const { epoch } = repo.snapshot()
    expect(repo.upsert(descriptor(), epoch).ok).toBe(true)
    expect(manager.deleteProfile('coding')).toBe(true)

    const recreated = manager.forProfile('coding')
    expect(recreated.snapshot().descriptors).toEqual([])
    recreated.close()
    manager.close()
  })

  it('fails closed on malformed restore rows instead of navigating or coercing them', () => {
    const { repo } = repository()
    expect(repo.open()).toBe(true)
    const db = new DatabaseSync(repo.file)
    db.prepare(`INSERT INTO tab_restore (
      restore_id, workspace_id, ordinal, url, title, pinned, created_at, updated_at
    ) VALUES ('malformed', 'workspace', 0, 'file:///tmp/secret', '', 0, 1, 1)`).run()
    db.close()

    expect(repo.snapshot()).toMatchObject({ degraded: true, descriptors: [] })
    expect(fs.existsSync(repo.file)).toBe(true)
    expect(repo.repair('reset-metadata')).toBe(true)
    expect(repo.snapshot()).toMatchObject({ degraded: false, descriptors: [] })
    expect(fs.readdirSync(path.dirname(repo.file)).some(name => name.startsWith('state.sqlite3.corrupt-'))).toBe(true)
    repo.close()
  })

  it('coordinates shared-handle repair, rotates epoch, and preserves restore preference', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-browser-state-repair-'))
    roots.push(root)
    const state = new BrowserStateRepositoryManager(root)
    const activity = new BrowserActivityRepositoryManager(root)
    const stateRepo = state.forProfile('coding')
    const oldEpoch = stateRepo.snapshot().epoch
    expect(stateRepo.setRestoreEnabled(false)).toBe(true)
    expect(activity.forProfile('coding').append({
      bindingGeneration: 1, category: 'navigate', certainty: 'completed', eventId: 'before-repair', phase: 'completed',
      source: 'agent', tabIncarnationId: 'tab-one', workspaceId: 'workspace-one'
    }).ok).toBe(true)

    expect(repairBrowserProfileMetadata('coding', 'reset-metadata', state, activity)).toEqual({
      activity: true, epoch: expect.any(String), metadata: true, ok: true, restoreEnabled: false
    })
    expect(stateRepo.snapshot().epoch).not.toBe(oldEpoch)
    expect(stateRepo.restoreEnabled()).toBe(false)
    expect(activity.forProfile('coding').append({
      bindingGeneration: 1, category: 'navigate', certainty: 'completed', eventId: 'after-repair', phase: 'completed',
      source: 'agent', tabIncarnationId: 'tab-two', workspaceId: 'workspace-one'
    }).ok).toBe(true)
    expect(activity.forProfile('coding').list().map(row => row.eventId)).toEqual(['after-repair'])
    activity.close()
    state.close()
  })

  it('quarantines sidecars with damaged metadata and destructive clear removes every copy', () => {
    const { repo } = repository()
    expect(repo.open()).toBe(true)
    expect(repo.setPermission('https://bank.example', 'notifications', 'allow')).toBe(true)
    repo.close()
    fs.writeFileSync(`${repo.file}-wal`, 'wal-canary')
    fs.writeFileSync(`${repo.file}-shm`, 'shm-canary')

    expect(repo.repair('reset-metadata')).toBe(true)
    const names = fs.readdirSync(path.dirname(repo.file))
    const quarantine = names.find(name => name.startsWith('state.sqlite3.corrupt-') && !name.endsWith('-wal') && !name.endsWith('-shm'))
    expect(quarantine).toBeTruthy()
    expect(names).toContain(`${quarantine}-wal`)
    expect(names).toContain(`${quarantine}-shm`)
    expect(repo.clearBrowsingMetadata({ history: true, permissions: true, transfers: true })).toBe(true)
    expect(fs.readdirSync(path.dirname(repo.file)).filter(name => name.startsWith('state.sqlite3.corrupt-'))).toEqual([])
    repo.close()
  })

  it('exports a main-owned copy of quarantined metadata and sidecars', () => {
    const { repo, root } = repository()
    expect(repo.open()).toBe(true)
    repo.close()
    fs.writeFileSync(`${repo.file}-wal`, 'wal-canary')
    expect(repo.repair('reset-metadata')).toBe(true)
    const quarantine = fs.readdirSync(path.dirname(repo.file))
      .find(name => name.startsWith('state.sqlite3.corrupt-') && !name.endsWith('-wal') && !name.endsWith('-shm'))!
    fs.rmSync(path.join(path.dirname(repo.file), `${quarantine}-shm`), { force: true })
    const destination = path.join(root, 'export.sqlite3')
    fs.writeFileSync(`${destination}-shm`, 'destination-canary')
    expect(repo.exportQuarantinedMetadata(destination)).toBe(true)
    expect(fs.existsSync(destination)).toBe(true)
    expect(fs.readFileSync(`${destination}-wal`, 'utf8')).toBe('wal-canary')
    expect(fs.readFileSync(`${destination}-shm`, 'utf8')).toBe('destination-canary')
    repo.close()
  })

  it('persists Electron camelCase permission names without weakening identifier bounds', () => {
    const { repo } = repository()

    expect(repo.setPermission('https://example.test', 'pointerLock', 'allow')).toBe(true)
    expect(repo.setPermission('https://example.test', 'midiSysex', 'deny')).toBe(true)
    expect(repo.permission('https://example.test', 'pointerLock')?.decision).toBe('allow')
    expect(repo.permission('https://example.test', 'midiSysex')?.decision).toBe('deny')
    expect(repo.setPermission('https://example.test', 'bad_permission', 'allow')).toBe(false)
    repo.close()
  })

  it('resumes an existing deletion tombstone and recreates the same profile empty', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-browser-state-resume-'))
    roots.push(root)
    const manager = new BrowserStateRepositoryManager(root)
    const repo = manager.forProfile('coding')
    expect(repo.upsert(descriptor(), repo.snapshot().epoch).ok).toBe(true)
    repo.close()
    const scopeRoot = path.dirname(path.dirname(repo.file))
    const scope = path.basename(path.dirname(repo.file))
    fs.writeFileSync(path.join(scopeRoot, `${scope}.delete-pending`), '{}')
    expect(() => manager.forProfile('coding')).toThrow('deletion is incomplete')
    expect(manager.deleteProfile('coding')).toBe(true)
    expect(manager.forProfile('coding').snapshot().descriptors).toEqual([])
    manager.close()
  })

  it('lists exact durable decisions and all known origins beyond the 500-row history view', () => {
    const { repo } = repository()
    expect(repo.open()).toBe(true)
    const db = new DatabaseSync(repo.file)
    const insert = db.prepare(`INSERT INTO visit_history (
      visit_id, workspace_id, url, origin, title, redaction_class, visited_at
    ) VALUES (?, 'workspace', ?, ?, '', 'none', ?)`)
    db.exec('BEGIN IMMEDIATE')
    for (let index = 0; index < 501; index += 1) {
      const origin = `https://site-${index}.example`
      insert.run(`visit-${index}`, `${origin}/`, origin, index + 1)
    }
    db.exec('COMMIT')
    db.close()
    expect(repo.history(500)).toHaveLength(500)
    expect(repo.origins()).toHaveLength(501)
    expect(repo.setPermission('https://permission.example', 'notifications', 'deny')).toBe(true)
    expect(repo.permissions()).toEqual([expect.objectContaining({ decision: 'deny', origin: 'https://permission.example' })])
    expect(repo.origins().some(row => row.origin === 'https://permission.example')).toBe(true)
    expect(repo.removePermission('https://permission.example', 'notifications')).toBe(true)
    expect(repo.permissions()).toEqual([])
    repo.close()
  })

  it('fails closed on a future schema rather than replacing it', () => {
    const { repo } = repository()
    fs.mkdirSync(path.dirname(repo.file), { recursive: true })
    const db = new DatabaseSync(repo.file)
    db.exec('PRAGMA user_version=99')
    db.close()

    expect(repo.open()).toBe(false)
    expect(repo.snapshot()).toMatchObject({ degraded: true, descriptors: [] })
    expect(fs.existsSync(repo.file)).toBe(true)
  })
})
