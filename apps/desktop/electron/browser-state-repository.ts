import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'

import { browserProfileScope } from './browser-activity-repository'

const SCHEMA_VERSION = 3
const MAX_ID = 160
const MAX_TITLE = 256
const MAX_URL = 4_096
const MAX_HISTORY_ROWS = 5_000
const MAX_HISTORY_AGE_MS = 30 * 24 * 60 * 60 * 1_000
const MAX_TRANSFER_ROWS = 5_000
const MAX_TRANSFER_AGE_MS = 30 * 24 * 60 * 60 * 1_000
const SENSITIVE_URL_KEY = /^(?:access[_-]?token|api[_-]?key|auth|authorization|code|credential|jwt|key|oauth|password|secret|session|sig|signature|ticket|token)$/i

export interface BrowserRestoreDescriptor {
  createdAt: number
  ordinal: number
  pinned: boolean
  restoreId: string
  restoredFromTabId: null | string
  title: string
  updatedAt: number
  url: string
  workspaceId: string
}

export interface BrowserRestoreInput {
  createdAt?: number
  ordinal: number
  pinned?: boolean
  restoreId: string
  restoredFromTabId?: null | string
  title?: string
  updatedAt?: number
  url: string
  workspaceId: string
}

export interface BrowserRestoreSnapshot {
  degraded: boolean
  descriptors: BrowserRestoreDescriptor[]
  epoch: string
  selectedRestoreId: null | string
}

export interface BrowserVisitRow {
  origin: string
  redactionClass: 'none' | 'sensitive'
  title: string
  url: string
  visitId: string
  visitedAt: number
  workspaceId: string
}

export interface BrowserPermissionDecision {
  decidedAt: number
  decision: 'allow' | 'deny'
  origin: string
  permission: string
}

export interface BrowserOriginSummary {
  lastUsedAt: number
  origin: string
}

export interface BrowserTransferInput {
  actor: 'human'
  byteSize?: null | number
  digest?: null | string
  direction: 'download' | 'upload'
  occurredAt?: number
  origin: string
  outcome: 'canceled' | 'completed' | 'failed'
  private?: boolean
  redactedName: string
  tabIncarnationId: string
  transferId?: string
}

export interface BrowserTransferRow {
  actor: 'human'
  byteSize: null | number
  digest: null | string
  direction: 'download' | 'upload'
  occurredAt: number
  origin: string
  outcome: 'canceled' | 'completed' | 'failed'
  redactedName: string
  tabIncarnationId: string
  transferId: string
}

function exactOrigin(value: unknown): string {
  if (typeof value !== 'string' || value.length > 320) {throw new Error('invalid origin')}
  const parsed = new URL(value)
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.pathname !== '/' || parsed.search || parsed.hash) {
    throw new Error('origin must contain only scheme and host')
  }
  return parsed.origin
}

function boundedPermission(value: unknown): string {
  if (typeof value !== 'string' || !/^[a-z][A-Za-z0-9-]{0,63}$/.test(value)) {throw new Error('invalid permission')}
  return value
}

function boundedId(value: unknown, required = false): null | string {
  if (value == null && !required) {return null}
  if (
    typeof value !== 'string' ||
    value.length < 1 ||
    value.length > MAX_ID ||
    [...value].some(character => {
      const code = character.charCodeAt(0)
      return code < 32 || code === 127
    })
  ) {
    throw new Error('invalid opaque identifier')
  }

  return value
}

function boundedTimestamp(value: unknown): number {
  if (!Number.isSafeInteger(value) || Number(value) < 0) {throw new Error('invalid timestamp')}
  return Number(value)
}

function boundedTitle(value: unknown): string {
  if (value == null) {return ''}
  if (typeof value !== 'string') {throw new Error('invalid title')}

  return [...value]
    .filter(character => {
      const code = character.charCodeAt(0)
      return code >= 32 && code !== 127
    })
    .join('')
    .slice(0, MAX_TITLE)
}

export function sanitizeBrowserPersistenceUrl(value: unknown): {
  origin: string
  redactionClass: 'none' | 'sensitive'
  restoreUrl: null | string
  visitUrl: string
} {
  if (typeof value !== 'string' || value.length < 1 || value.length > MAX_URL) {
    throw new Error('invalid browser URL')
  }

  const parsed = new URL(value)

  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) {
    throw new Error('browser URL is not persistable')
  }

  const sensitive = [...parsed.searchParams.keys()].some(key => SENSITIVE_URL_KEY.test(key))
  parsed.hash = ''

  return sensitive
    ? { origin: parsed.origin, redactionClass: 'sensitive', restoreUrl: null, visitUrl: parsed.origin }
    : { origin: parsed.origin, redactionClass: 'none', restoreUrl: parsed.toString(), visitUrl: parsed.toString() }
}

export class BrowserStateRepository {
  readonly file: string
  #db: DatabaseSync | null = null
  #degraded = false
  #epoch = crypto.randomUUID()
  #now: () => number

  constructor(userData: string, profile: string, now: () => number = Date.now) {
    this.file = path.join(userData, 'browser', 'v1', browserProfileScope(profile), 'state.sqlite3')
    this.#now = now
  }

  get degraded(): boolean {return this.#degraded}

  open(): boolean {
    if (this.#db) {return true}
    if (this.#degraded) {return false}

    let opening: DatabaseSync | null = null
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true })
      const db = new DatabaseSync(this.file)
      opening = db
      db.exec('PRAGMA secure_delete=ON; PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;')
      if (String(db.prepare('PRAGMA quick_check').get()?.quick_check ?? '') !== 'ok') {
        throw new Error('browser metadata integrity check failed')
      }
      const version = Number(db.prepare('PRAGMA user_version').get()?.user_version ?? 0)

      if (version > SCHEMA_VERSION) {throw new Error('unknown future browser metadata schema')}
      db.exec(`
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS browser_workspace (
          workspace_id TEXT PRIMARY KEY,
          selected_restore_id TEXT,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tab_restore (
          restore_id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL,
          ordinal INTEGER NOT NULL,
          url TEXT NOT NULL,
          title TEXT NOT NULL,
          pinned INTEGER NOT NULL CHECK(pinned IN (0,1)),
          restored_from_tab_id TEXT,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS tab_restore_workspace ON tab_restore(workspace_id, ordinal, created_at);
        CREATE TABLE IF NOT EXISTS visit_history (
          visit_id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL,
          url TEXT NOT NULL,
          origin TEXT NOT NULL,
          title TEXT NOT NULL,
          redaction_class TEXT NOT NULL CHECK(redaction_class IN ('none','sensitive')),
          visited_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS visit_history_visited ON visit_history(visited_at, visit_id);
        CREATE TABLE IF NOT EXISTS browser_preference (
          id TEXT PRIMARY KEY CHECK(id='profile'),
          restore_enabled INTEGER NOT NULL CHECK(restore_enabled IN (0,1)),
          updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS permission_decision (
          origin TEXT NOT NULL,
          permission TEXT NOT NULL,
          decision TEXT NOT NULL CHECK(decision IN ('allow','deny')),
          persistence_class TEXT NOT NULL CHECK(persistence_class='durable'),
          decided_at INTEGER NOT NULL,
          PRIMARY KEY(origin, permission)
        );
        CREATE TABLE IF NOT EXISTS transfer_provenance (
          transfer_id TEXT PRIMARY KEY,
          direction TEXT NOT NULL CHECK(direction IN ('download','upload')),
          actor TEXT NOT NULL CHECK(actor='human'),
          origin TEXT NOT NULL,
          tab_incarnation_id TEXT NOT NULL,
          redacted_name TEXT NOT NULL,
          byte_size INTEGER,
          digest TEXT,
          outcome TEXT NOT NULL CHECK(outcome IN ('canceled','completed','failed')),
          occurred_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS transfer_provenance_occurred ON transfer_provenance(occurred_at, transfer_id);
        PRAGMA user_version=${SCHEMA_VERSION};
        COMMIT;
      `)
      this.#db = db
      opening = null
      this.prune()
      return true
    } catch {
      this.#degraded = true
      opening?.close()
      this.#db?.close()
      this.#db = null
      return false
    }
  }

  snapshot(): BrowserRestoreSnapshot {
    if (!this.open() || !this.#db) {
      return { degraded: true, descriptors: [], epoch: this.#epoch, selectedRestoreId: null }
    }

    try {
      const descriptors = this.#db.prepare('SELECT * FROM tab_restore ORDER BY workspace_id, ordinal, created_at').all()
        .map(row => this.#restoreFromSql(row as Record<string, unknown>))
      const selected = this.#db.prepare(`SELECT selected_restore_id FROM browser_workspace
        WHERE selected_restore_id IS NOT NULL ORDER BY updated_at DESC LIMIT 1`).get()

      return {
        degraded: false,
        descriptors,
        epoch: this.#epoch,
        selectedRestoreId: selected?.selected_restore_id == null ? null : String(selected.selected_restore_id)
      }
    } catch {
      this.#degraded = true
      return { degraded: true, descriptors: [], epoch: this.#epoch, selectedRestoreId: null }
    }
  }

  upsert(input: BrowserRestoreInput, epoch: string): { ok: boolean; persisted?: BrowserRestoreDescriptor } {
    if (!this.#acceptEpoch(epoch) || !this.#db) {return { ok: false }}
    if (!this.restoreEnabled()) {return { ok: false }}

    try {
      const now = this.#now()
      const restoreId = boundedId(input.restoreId, true)!
      const workspaceId = boundedId(input.workspaceId, true)!
      const restoredFromTabId = boundedId(input.restoredFromTabId)
      const ordinal = Number(input.ordinal)
      const parsed = sanitizeBrowserPersistenceUrl(input.url)

      if (!parsed.restoreUrl || !Number.isSafeInteger(ordinal) || ordinal < 0 || ordinal > 10_000) {
        return { ok: false }
      }

      const title = boundedTitle(input.title)
      const createdAt = boundedTimestamp(input.createdAt ?? now)
      const updatedAt = boundedTimestamp(input.updatedAt ?? now)
      const previous = this.#db.prepare('SELECT url FROM tab_restore WHERE restore_id=?').get(restoreId)

      this.#db.exec('BEGIN IMMEDIATE')
      this.#db.prepare(`INSERT INTO browser_workspace (workspace_id, selected_restore_id, created_at, updated_at)
        VALUES (?, NULL, ?, ?) ON CONFLICT(workspace_id) DO UPDATE SET updated_at=excluded.updated_at`
      ).run(workspaceId, createdAt, updatedAt)
      this.#db.prepare(`INSERT INTO tab_restore (
        restore_id, workspace_id, ordinal, url, title, pinned, restored_from_tab_id, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(restore_id) DO UPDATE SET
        workspace_id=excluded.workspace_id, ordinal=excluded.ordinal, url=excluded.url, title=excluded.title,
        pinned=excluded.pinned, restored_from_tab_id=excluded.restored_from_tab_id, updated_at=excluded.updated_at`
      ).run(
        restoreId, workspaceId, ordinal, parsed.restoreUrl, title, input.pinned === true ? 1 : 0,
        restoredFromTabId, createdAt, updatedAt
      )

      if (!previous || String(previous.url) !== parsed.restoreUrl) {
        this.#insertVisit({ title, url: parsed.restoreUrl, visitedAt: updatedAt, workspaceId })
      }

      this.#pruneInTransaction()
      this.#db.exec('COMMIT')

      return {
        ok: true,
        persisted: {
          createdAt,
          ordinal,
          pinned: input.pinned === true,
          restoreId,
          restoredFromTabId,
          title,
          updatedAt,
          url: parsed.restoreUrl,
          workspaceId
        }
      }
    } catch {
      try {this.#db?.exec('ROLLBACK')} catch { /* already rolled back */ }
      this.#degraded = true
      return { ok: false }
    }
  }

  remove(restoreId: string, epoch: string): boolean {
    if (!this.#acceptEpoch(epoch) || !this.#db) {return false}

    try {
      const id = boundedId(restoreId, true)!
      this.#db.exec('BEGIN IMMEDIATE')
      this.#db.prepare('UPDATE browser_workspace SET selected_restore_id=NULL, updated_at=? WHERE selected_restore_id=?').run(this.#now(), id)
      this.#db.prepare('DELETE FROM tab_restore WHERE restore_id=?').run(id)
      this.#db.exec('COMMIT')
      return true
    } catch {
      try {this.#db?.exec('ROLLBACK')} catch { /* already rolled back */ }
      this.#degraded = true
      return false
    }
  }

  select(workspaceId: string, restoreId: null | string, epoch: string): boolean {
    if (!this.#acceptEpoch(epoch) || !this.#db) {return false}

    try {
      const workspace = boundedId(workspaceId, true)!
      const selected = boundedId(restoreId)

      if (selected && !this.#db.prepare('SELECT 1 FROM tab_restore WHERE restore_id=? AND workspace_id=?').get(selected, workspace)) {
        return false
      }

      const now = this.#now()
      this.#db.prepare(`INSERT INTO browser_workspace (workspace_id, selected_restore_id, created_at, updated_at)
        VALUES (?, ?, ?, ?) ON CONFLICT(workspace_id) DO UPDATE SET
        selected_restore_id=excluded.selected_restore_id, updated_at=excluded.updated_at`
      ).run(workspace, selected, now, now)
      return true
    } catch {
      this.#degraded = true
      return false
    }
  }

  history(limit = 200): BrowserVisitRow[] {
    if (!this.open() || !this.#db) {return []}
    const boundedLimit = Math.max(1, Math.min(500, Math.trunc(limit)))

    try {
      return this.#db.prepare('SELECT * FROM visit_history ORDER BY visited_at DESC, visit_id DESC LIMIT ?').all(boundedLimit)
        .map(row => ({
          origin: String(row.origin),
          redactionClass: String(row.redaction_class) as BrowserVisitRow['redactionClass'],
          title: String(row.title),
          url: String(row.url),
          visitId: String(row.visit_id),
          visitedAt: Number(row.visited_at),
          workspaceId: String(row.workspace_id)
        })).reverse()
    } catch {
      this.#degraded = true
      return []
    }
  }

  origins(): BrowserOriginSummary[] {
    if (!this.open() || !this.#db) {return []}
    try {
      return this.#db.prepare(`SELECT origin, MAX(last_used_at) AS last_used_at FROM (
        SELECT origin, visited_at AS last_used_at FROM visit_history
        UNION ALL SELECT origin, decided_at AS last_used_at FROM permission_decision
        UNION ALL SELECT origin, occurred_at AS last_used_at FROM transfer_provenance
      ) GROUP BY origin ORDER BY last_used_at DESC, origin ASC LIMIT ${MAX_HISTORY_ROWS + MAX_TRANSFER_ROWS}`).all().map(row => ({
        lastUsedAt: Number(row.last_used_at),
        origin: String(row.origin)
      }))
    } catch {
      this.#degraded = true
      return []
    }
  }

  restoreEnabled(): boolean {
    if (!this.open() || !this.#db) {return false}
    try {return this.#db.prepare("SELECT restore_enabled FROM browser_preference WHERE id='profile'").get()?.restore_enabled !== 0} catch {return false}
  }

  setRestoreEnabled(enabled: boolean): boolean {
    if (!this.open() || !this.#db || typeof enabled !== 'boolean') {return false}
    try {
      this.#db.exec('BEGIN IMMEDIATE')
      this.#db.prepare(`INSERT INTO browser_preference(id, restore_enabled, updated_at) VALUES('profile', ?, ?)
        ON CONFLICT(id) DO UPDATE SET restore_enabled=excluded.restore_enabled, updated_at=excluded.updated_at`).run(enabled ? 1 : 0, this.#now())
      if (!enabled) {
        this.#db.exec('DELETE FROM tab_restore; UPDATE browser_workspace SET selected_restore_id=NULL;')
        this.#epoch = crypto.randomUUID()
      }
      this.#db.exec('COMMIT')
      return true
    } catch {
      try {this.#db?.exec('ROLLBACK')} catch { /* no transaction */ }
      this.#degraded = true
      return false
    }
  }

  permission(origin: string, permission: string): BrowserPermissionDecision | null {
    if (!this.open() || !this.#db) {return null}
    try {
      const row = this.#db.prepare('SELECT * FROM permission_decision WHERE origin=? AND permission=?').get(exactOrigin(origin), boundedPermission(permission))
      return row ? { decidedAt: Number(row.decided_at), decision: String(row.decision) as 'allow' | 'deny', origin: String(row.origin), permission: String(row.permission) } : null
    } catch {return null}
  }

  setPermission(origin: string, permission: string, decision: 'allow' | 'deny', persistence: 'durable' | 'session' = 'durable'): boolean {
    if (persistence === 'session' || !this.open() || !this.#db || !['allow', 'deny'].includes(decision)) {return false}
    try {
      this.#db.prepare(`INSERT INTO permission_decision(origin, permission, decision, persistence_class, decided_at)
        VALUES(?, ?, ?, 'durable', ?) ON CONFLICT(origin, permission) DO UPDATE SET decision=excluded.decision, decided_at=excluded.decided_at`
      ).run(exactOrigin(origin), boundedPermission(permission), decision, this.#now())
      return true
    } catch {return false}
  }

  permissions(limit = 500): BrowserPermissionDecision[] {
    if (!this.open() || !this.#db) {return []}
    const count = Math.max(1, Math.min(5_000, Math.trunc(limit)))
    try {
      return this.#db.prepare('SELECT * FROM permission_decision ORDER BY decided_at DESC, origin ASC, permission ASC LIMIT ?').all(count).map(row => ({
        decidedAt: Number(row.decided_at),
        decision: String(row.decision) as BrowserPermissionDecision['decision'],
        origin: String(row.origin),
        permission: String(row.permission)
      }))
    } catch {return []}
  }

  removePermission(origin: string, permission: string): boolean {
    if (!this.open() || !this.#db) {return false}
    try {
      this.#db.prepare('DELETE FROM permission_decision WHERE origin=? AND permission=?').run(exactOrigin(origin), boundedPermission(permission))
      return true
    } catch {return false}
  }

  clearPermissions(origin?: string): boolean {
    if (!this.open() || !this.#db) {return false}
    try {
      if (origin) {this.#db.prepare('DELETE FROM permission_decision WHERE origin=?').run(exactOrigin(origin))}
      else {this.#db.exec('DELETE FROM permission_decision')}
      return true
    } catch {return false}
  }

  appendTransfer(input: BrowserTransferInput): { ok: boolean; row?: BrowserTransferRow } {
    if (!this.open() || !this.#db || !input || input.private === true) {return { ok: false }}
    try {
      const redactedName = boundedTitle(input.redactedName)
      if (!redactedName || redactedName !== path.basename(redactedName) || /[\\/]/.test(redactedName)) {return { ok: false }}
      if (input.actor !== 'human' || !['download', 'upload'].includes(input.direction) || !['canceled', 'completed', 'failed'].includes(input.outcome)) {return { ok: false }}
      const row: BrowserTransferRow = {
        actor: 'human', byteSize: input.byteSize == null ? null : boundedTimestamp(input.byteSize),
        digest: input.digest == null ? null : String(input.digest), direction: input.direction,
        occurredAt: boundedTimestamp(input.occurredAt ?? this.#now()), origin: exactOrigin(input.origin), outcome: input.outcome,
        redactedName, tabIncarnationId: boundedId(input.tabIncarnationId, true)!, transferId: boundedId(input.transferId ?? crypto.randomUUID(), true)!
      }
      if (row.digest && !/^sha256:[a-f0-9]{64}$/.test(row.digest)) {return { ok: false }}
      this.#db.prepare(`INSERT INTO transfer_provenance(transfer_id,direction,actor,origin,tab_incarnation_id,redacted_name,byte_size,digest,outcome,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)`).run(row.transferId,row.direction,row.actor,row.origin,row.tabIncarnationId,row.redactedName,row.byteSize,row.digest,row.outcome,row.occurredAt)
      this.#pruneTransfersInTransaction()
      return { ok: true, row }
    } catch {return { ok: false }}
  }

  transfers(limit = 200): BrowserTransferRow[] {
    if (!this.open() || !this.#db) {return []}
    const count = Math.max(1, Math.min(500, Math.trunc(limit)))
    try {return this.#db.prepare('SELECT * FROM transfer_provenance ORDER BY occurred_at DESC, transfer_id DESC LIMIT ?').all(count).map(row => ({
      actor: 'human' as const, byteSize: row.byte_size == null ? null : Number(row.byte_size), digest: row.digest == null ? null : String(row.digest),
      direction: String(row.direction) as BrowserTransferRow['direction'], occurredAt: Number(row.occurred_at), origin: String(row.origin),
      outcome: String(row.outcome) as BrowserTransferRow['outcome'], redactedName: String(row.redacted_name), tabIncarnationId: String(row.tab_incarnation_id), transferId: String(row.transfer_id)
    })).reverse()} catch {return []}
  }

  clearBrowsingMetadata(options: { history?: boolean; permissions?: boolean; transfers?: boolean }): boolean {
    if (!this.open() || !this.#db) {return false}
    try {
      this.#db.exec('BEGIN IMMEDIATE')
      if (options.history) {this.#db.exec('DELETE FROM visit_history')}
      if (options.permissions) {this.#db.exec('DELETE FROM permission_decision')}
      if (options.transfers) {this.#db.exec('DELETE FROM transfer_provenance')}
      this.#db.exec('COMMIT')
      if (options.history && options.permissions && options.transfers) {this.#removeQuarantinedMetadata()}
      return true
    } catch {try {this.#db?.exec('ROLLBACK')} catch { /* no transaction */ }; return false}
  }

  repair(mode: 'retry' | 'reset-metadata'): boolean {
    const restoreEnabled = this.#readRestorePreference()
    this.close()
    this.#degraded = false
    if (mode === 'reset-metadata') {
      try {
        const quarantine = `${this.file}.corrupt-${this.#now()}-${crypto.randomUUID()}`
        if (fs.existsSync(this.file)) {fs.renameSync(this.file, quarantine)}
        for (const suffix of ['-wal', '-shm']) {
          if (fs.existsSync(`${this.file}${suffix}`)) {fs.renameSync(`${this.file}${suffix}`, `${quarantine}${suffix}`)}
        }
      } catch {this.#degraded = true; return false}
    }
    const opened = this.open()
    if (opened && mode === 'reset-metadata') {
      this.#epoch = crypto.randomUUID()
      if (restoreEnabled === false && !this.setRestoreEnabled(false)) {return false}
    }
    return opened
  }

  exportQuarantinedMetadata(destination: string): boolean {
    try {
      const directory = path.dirname(this.file)
      const prefix = `${path.basename(this.file)}.corrupt-`
      const candidates = fs.readdirSync(directory)
        .filter(name => name.startsWith(prefix) && !name.endsWith('-wal') && !name.endsWith('-shm'))
        .sort()
      const source = candidates.at(-1) ?? (fs.existsSync(this.file) ? path.basename(this.file) : null)
      if (!source || typeof destination !== 'string' || !destination) {return false}
      fs.copyFileSync(path.join(directory, source), destination)
      for (const suffix of ['-wal', '-shm']) {
        const sidecar = path.join(directory, `${source}${suffix}`)
        if (fs.existsSync(sidecar)) {fs.copyFileSync(sidecar, `${destination}${suffix}`)}
      }
      return true
    } catch {return false}
  }

  resetWorkspace(workspaceId: string, includeHistory = false): { epoch: string; ok: boolean } {
    if (!this.open() || !this.#db) {return { epoch: this.#epoch, ok: false }}

    try {
      const workspace = boundedId(workspaceId, true)!
      this.#db.exec('BEGIN IMMEDIATE')
      this.#db.prepare('DELETE FROM tab_restore WHERE workspace_id=?').run(workspace)
      if (includeHistory) {this.#db.prepare('DELETE FROM visit_history WHERE workspace_id=?').run(workspace)}
      this.#db.prepare('DELETE FROM browser_workspace WHERE workspace_id=?').run(workspace)
      this.#db.exec('COMMIT')
      this.#epoch = crypto.randomUUID()
      this.#destructiveMaintenance()
      return { epoch: this.#epoch, ok: true }
    } catch {
      try {this.#db?.exec('ROLLBACK')} catch { /* already rolled back */ }
      this.#degraded = true
      return { epoch: this.#epoch, ok: false }
    }
  }

  close(): void {
    this.#db?.close()
    this.#db = null
  }

  prune(): boolean {
    if (!this.open() || !this.#db) {return false}

    try {
      this.#db.exec('BEGIN IMMEDIATE')
      this.#pruneInTransaction()
      this.#db.exec('COMMIT')
      this.#destructiveMaintenance()
      return true
    } catch {
      try {this.#db?.exec('ROLLBACK')} catch { /* already rolled back */ }
      this.#degraded = true
      return false
    }
  }

  #acceptEpoch(epoch: string): boolean {
    return typeof epoch === 'string' && epoch === this.#epoch && this.open()
  }

  #readRestorePreference(): null | boolean {
    try {
      if (this.#db) {
        const row = this.#db.prepare("SELECT restore_enabled FROM browser_preference WHERE id='profile'").get()
        return row ? row.restore_enabled !== 0 : null
      }
      if (!fs.existsSync(this.file)) {return null}
      const db = new DatabaseSync(this.file, { readOnly: true })
      try {
        const row = db.prepare("SELECT restore_enabled FROM browser_preference WHERE id='profile'").get()
        return row ? row.restore_enabled !== 0 : null
      } finally {db.close()}
    } catch {return null}
  }

  #removeQuarantinedMetadata(): void {
    const directory = path.dirname(this.file)
    const prefix = `${path.basename(this.file)}.corrupt-`
    for (const name of fs.readdirSync(directory)) {
      if (name.startsWith(prefix)) {fs.rmSync(path.join(directory, name), { force: true })}
    }
  }

  #insertVisit(input: { title: string; url: string; visitedAt: number; workspaceId: string }): void {
    const parsed = sanitizeBrowserPersistenceUrl(input.url)
    this.#db!.prepare(`INSERT INTO visit_history (
      visit_id, workspace_id, url, origin, title, redaction_class, visited_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?)`).run(
      crypto.randomUUID(), input.workspaceId, parsed.visitUrl, parsed.origin, input.title,
      parsed.redactionClass, input.visitedAt
    )
  }

  #pruneInTransaction(): void {
    const cutoff = this.#now() - MAX_HISTORY_AGE_MS
    this.#db!.prepare('DELETE FROM visit_history WHERE visited_at < ?').run(cutoff)
    this.#db!.exec(`DELETE FROM visit_history WHERE visit_id IN (
      SELECT visit_id FROM visit_history ORDER BY visited_at ASC, visit_id ASC
      LIMIT (SELECT CASE WHEN COUNT(*) > ${MAX_HISTORY_ROWS} THEN COUNT(*) - ${MAX_HISTORY_ROWS} ELSE 0 END FROM visit_history)
    )`)
    this.#pruneTransfersInTransaction()
  }

  #pruneTransfersInTransaction(): void {
    const cutoff = this.#now() - MAX_TRANSFER_AGE_MS
    this.#db!.prepare('DELETE FROM transfer_provenance WHERE occurred_at < ?').run(cutoff)
    this.#db!.exec(`DELETE FROM transfer_provenance WHERE transfer_id IN (
      SELECT transfer_id FROM transfer_provenance ORDER BY occurred_at ASC, transfer_id ASC
      LIMIT (SELECT CASE WHEN COUNT(*) > ${MAX_TRANSFER_ROWS} THEN COUNT(*) - ${MAX_TRANSFER_ROWS} ELSE 0 END FROM transfer_provenance)
    )`)
  }

  #restoreFromSql(row: Record<string, unknown>): BrowserRestoreDescriptor {
    const restoreId = boundedId(row.restore_id, true)!
    const workspaceId = boundedId(row.workspace_id, true)!
    const restoredFromTabId = boundedId(row.restored_from_tab_id)
    const createdAt = boundedTimestamp(row.created_at)
    const updatedAt = boundedTimestamp(row.updated_at)
    const ordinal = Number(row.ordinal)
    const title = boundedTitle(row.title)
    const parsed = sanitizeBrowserPersistenceUrl(row.url)

    if (
      !parsed.restoreUrl || parsed.restoreUrl !== row.url || title !== row.title ||
      !Number.isSafeInteger(ordinal) || ordinal < 0 || ordinal > 10_000 ||
      (row.pinned !== 0 && row.pinned !== 1)
    ) {
      throw new Error('invalid browser restore row')
    }

    return {
      createdAt,
      ordinal,
      pinned: row.pinned === 1,
      restoreId,
      restoredFromTabId,
      title,
      updatedAt,
      url: parsed.restoreUrl,
      workspaceId
    }
  }

  #destructiveMaintenance(): void {
    this.#db!.exec('PRAGMA wal_checkpoint(TRUNCATE)')
  }
}

export class BrowserStateRepositoryManager {
  #repositories = new Map<string, BrowserStateRepository>()

  constructor(private readonly userData: string) {}

  forProfile(profile: string): BrowserStateRepository {
    const scope = browserProfileScope(profile)
    const tombstone = path.join(this.userData, 'browser', 'v1', `${scope}.delete-pending`)

    if (fs.existsSync(tombstone)) {throw new Error('browser profile deletion is incomplete')}
    let repository = this.#repositories.get(scope)

    if (!repository) {
      repository = new BrowserStateRepository(this.userData, profile)
      this.#repositories.set(scope, repository)
    }

    return repository
  }

  deleteProfile(profile: string): boolean {
    const scope = browserProfileScope(profile)
    this.#repositories.get(scope)?.close()
    this.#repositories.delete(scope)
    const root = path.join(this.userData, 'browser', 'v1')
    const directory = path.join(root, scope)
    const tombstone = path.join(root, `${scope}.delete-pending`)

    try {
      fs.mkdirSync(root, { recursive: true })
      if (!fs.existsSync(tombstone)) {
        fs.writeFileSync(tombstone, JSON.stringify({ scope, startedAt: Date.now() }), { flag: 'wx' })
      }
      fs.rmSync(directory, { force: true, recursive: true })
      fs.rmSync(tombstone, { force: true })
      return true
    } catch {
      return false
    }
  }

  close(): void {
    for (const repository of this.#repositories.values()) {repository.close()}
    this.#repositories.clear()
  }
}
