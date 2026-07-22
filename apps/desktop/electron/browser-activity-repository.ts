import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'

const PROFILE_SCOPE_DOMAIN = 'hermes-browser-profile-v1\0'
// Schema v3 is shared with browser-state-repository.ts. Each main-process
// repository creates its own tables idempotently in the same profile database.
const SCHEMA_VERSION = 3
const MAX_ROWS = 5_000
const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1_000
const MAX_ID = 160
const MAX_SITE = 320
const TERMINAL_PHASES = new Set(['completed', 'failed', 'not_started', 'outcome_unknown', 'late_frame_rejected'])
const SOURCES = new Set(['agent', 'consent', 'human', 'policy', 'system', 'transport'])
const CATEGORIES = new Set(['action', 'hand-back', 'navigate', 'pause', 'snapshot', 'stop', 'takeover'])

const PHASES = new Set([
  'admitted',
  'awaiting_consent',
  'completed',
  'dispatched',
  'failed',
  'late_frame_rejected',
  'not_started',
  'outcome_unknown',
  'proposed'
])

const CERTAINTIES = new Set(['completed', 'failed', 'not_started', 'outcome_unknown'])

const REASONS = new Set([
  'app_restart',
  'consent_denied',
  'consent_expired',
  'policy_denied',
  'requested',
  'retired',
  'unknown'
])

export interface BrowserActivityInput {
  bindingGeneration: number
  category: string
  certainty?: null | string
  checkpointAttempted?: boolean
  checkpointAvailableAtCapture?: boolean
  consentId?: null | string
  createdAt?: number
  eventId?: string
  operationId?: null | string
  phase: string
  private?: boolean
  reasonCode?: null | string
  sessionId?: null | string
  sessionLineageId?: null | string
  siteIdentity?: null | string
  source: string
  tabIncarnationId: string
  terminalAt?: null | number
  workspaceId: string
}

export interface BrowserActivityRow extends BrowserActivityInput {
  createdAt: number
  eventId: string
  private: false
  sequence: number
}

export interface BrowserActivityResult {
  error?: 'degraded' | 'invalid'
  ok: boolean
  row?: BrowserActivityRow
}

export function browserProfileScope(profile: string): string {
  const normalized = String(profile || '').trim().toLowerCase() || 'default'

  if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(normalized)) {
    throw new Error('invalid browser profile')
  }

  return crypto.createHash('sha256').update(`${PROFILE_SCOPE_DOMAIN}${normalized}`, 'utf8').digest('base64url').slice(0, 22)
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

function boundedTimestamp(value: unknown, required = true): null | number {
  if (value == null && !required) {return null}

  if (!Number.isSafeInteger(value) || Number(value) < 0) {throw new Error('invalid timestamp')}

  return Number(value)
}

function siteIdentity(value: unknown): null | string {
  if (value == null) {return null}

  if (typeof value !== 'string' || value.length < 1 || value.length > MAX_SITE) {throw new Error('invalid site identity')}
  const parsed = new URL(value)

  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.pathname !== '/' || parsed.search || parsed.hash) {
    throw new Error('site identity must contain only scheme and host')
  }

  return parsed.origin
}

function validateInput(input: BrowserActivityInput, now: number): Omit<BrowserActivityRow, 'sequence'> {
  if (!input || typeof input !== 'object' || input.private === true) {throw new Error('private activity is not durable')}

  if (!Number.isSafeInteger(input.bindingGeneration) || input.bindingGeneration < 0) {throw new Error('invalid generation')}

  if (!SOURCES.has(input.source) || !CATEGORIES.has(input.category) || !PHASES.has(input.phase)) {throw new Error('invalid enum')}

  if (input.certainty != null && !CERTAINTIES.has(input.certainty)) {throw new Error('invalid certainty')}

  if (input.reasonCode != null && !REASONS.has(input.reasonCode)) {throw new Error('invalid reason')}

  if (TERMINAL_PHASES.has(input.phase) && input.certainty == null) {throw new Error('terminal phase requires certainty')}

  if (!TERMINAL_PHASES.has(input.phase) && (input.certainty != null || input.terminalAt != null)) {throw new Error('nonterminal phase cannot claim an outcome')}

  return {
    bindingGeneration: input.bindingGeneration,
    category: input.category,
    certainty: input.certainty ?? null,
    checkpointAttempted: input.checkpointAttempted === true,
    checkpointAvailableAtCapture: input.checkpointAvailableAtCapture === true,
    consentId: boundedId(input.consentId),
    createdAt: boundedTimestamp(input.createdAt ?? now)!,
    eventId: boundedId(input.eventId ?? crypto.randomUUID(), true)!,
    operationId: boundedId(input.operationId),
    phase: input.phase,
    private: false,
    reasonCode: input.reasonCode ?? null,
    sessionId: boundedId(input.sessionId),
    sessionLineageId: boundedId(input.sessionLineageId),
    siteIdentity: siteIdentity(input.siteIdentity),
    source: input.source,
    tabIncarnationId: boundedId(input.tabIncarnationId, true)!,
    terminalAt: boundedTimestamp(input.terminalAt ?? (TERMINAL_PHASES.has(input.phase) ? now : null), false),
    workspaceId: boundedId(input.workspaceId, true)!
  }
}

function fromSql(row: Record<string, unknown>): BrowserActivityRow {
  return {
    bindingGeneration: Number(row.binding_generation),
    category: String(row.category),
    certainty: row.certainty == null ? null : String(row.certainty),
    checkpointAttempted: Boolean(row.checkpoint_attempted),
    checkpointAvailableAtCapture: Boolean(row.checkpoint_available_at_capture),
    consentId: row.consent_id == null ? null : String(row.consent_id),
    createdAt: Number(row.created_at),
    eventId: String(row.event_id),
    operationId: row.operation_id == null ? null : String(row.operation_id),
    phase: String(row.phase),
    private: false,
    reasonCode: row.reason_code == null ? null : String(row.reason_code),
    sequence: Number(row.sequence),
    sessionId: row.session_id == null ? null : String(row.session_id),
    sessionLineageId: row.session_lineage_id == null ? null : String(row.session_lineage_id),
    siteIdentity: row.site_identity == null ? null : String(row.site_identity),
    source: String(row.source),
    tabIncarnationId: String(row.tab_incarnation_id),
    terminalAt: row.terminal_at == null ? null : Number(row.terminal_at),
    workspaceId: String(row.workspace_id)
  }
}

export class BrowserActivityRepository {
  readonly file: string
  #db: DatabaseSync | null = null
  #degraded = false
  #now: () => number
  #pruneTimer: ReturnType<typeof setInterval> | null = null

  constructor(userData: string, profile: string, now: () => number = Date.now) {
    this.file = path.join(userData, 'browser', 'v1', browserProfileScope(profile), 'state.sqlite3')
    this.#now = now
  }

  get degraded(): boolean { return this.#degraded }

  open(): boolean {
    if (this.#db) {return true}

    if (this.#degraded) {return false}

    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true })
      const db = new DatabaseSync(this.file)
      db.exec('PRAGMA secure_delete=ON; PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;')
      const version = Number(db.prepare('PRAGMA user_version').get()?.user_version ?? 0)

      if (version > SCHEMA_VERSION) {throw new Error('unknown future browser metadata schema')}
      db.exec(`
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS browser_action_event (
          event_id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL,
          sequence INTEGER NOT NULL,
          tab_incarnation_id TEXT NOT NULL,
          binding_generation INTEGER NOT NULL,
          operation_id TEXT,
          consent_id TEXT,
          session_id TEXT,
          session_lineage_id TEXT,
          source TEXT NOT NULL,
          category TEXT NOT NULL,
          phase TEXT NOT NULL,
          certainty TEXT,
          reason_code TEXT,
          created_at INTEGER NOT NULL,
          terminal_at INTEGER,
          site_identity TEXT,
          checkpoint_attempted INTEGER NOT NULL CHECK(checkpoint_attempted IN (0,1)),
          checkpoint_available_at_capture INTEGER NOT NULL CHECK(checkpoint_available_at_capture IN (0,1)),
          private INTEGER NOT NULL CHECK(private = 0),
          UNIQUE(workspace_id, sequence)
        );
        CREATE INDEX IF NOT EXISTS browser_action_event_created ON browser_action_event(created_at, sequence);
        CREATE INDEX IF NOT EXISTS browser_action_event_session ON browser_action_event(session_id);
        PRAGMA user_version=${SCHEMA_VERSION};
        COMMIT;
      `)
      this.#db = db
      this.#settleRestart()
      this.prune()
      this.#pruneTimer = setInterval(() => this.prune(), 60 * 60 * 1000)
      this.#pruneTimer.unref?.()

      return true
    } catch {
      this.#degraded = true
      this.#db?.close()
      this.#db = null

      return false
    }
  }

  append(input: BrowserActivityInput): BrowserActivityResult {
    if (input?.private === true) {return { error: 'invalid', ok: false }}

    if (!this.open() || !this.#db) {return { error: 'degraded', ok: false }}

    try {
      const row = validateInput(input, this.#now())
      this.#db.exec('BEGIN IMMEDIATE')

      const sequence = Number(
        this.#db.prepare('SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM browser_action_event WHERE workspace_id = ?').get(row.workspaceId)?.sequence
      )

      this.#db.prepare(`INSERT INTO browser_action_event (
        event_id, workspace_id, sequence, tab_incarnation_id, binding_generation, operation_id, consent_id,
        session_id, session_lineage_id, source, category, phase, certainty, reason_code, created_at,
        terminal_at, site_identity, checkpoint_attempted, checkpoint_available_at_capture, private
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)`).run(
        row.eventId, row.workspaceId, sequence, row.tabIncarnationId, row.bindingGeneration, row.operationId,
        row.consentId, row.sessionId, row.sessionLineageId, row.source, row.category, row.phase, row.certainty,
        row.reasonCode, row.createdAt, row.terminalAt, row.siteIdentity, row.checkpointAttempted ? 1 : 0,
        row.checkpointAvailableAtCapture ? 1 : 0
      )
      this.#pruneInTransaction()
      this.#db.exec('COMMIT')

      return { ok: true, row: { ...row, sequence } }
    } catch {
      try { this.#db?.exec('ROLLBACK') } catch { /* already rolled back */ }
      this.#degraded = true

      return { error: 'degraded', ok: false }
    }
  }

  list(limit = 200, workspaceId?: string): BrowserActivityRow[] {
    if (!this.open() || !this.#db) {return []}
    const boundedLimit = Math.max(1, Math.min(500, Math.trunc(limit)))

    try {
      const rows = workspaceId
        ? this.#db.prepare('SELECT * FROM browser_action_event WHERE workspace_id = ? ORDER BY created_at DESC, sequence DESC LIMIT ?').all(boundedId(workspaceId, true), boundedLimit)
        : this.#db.prepare('SELECT * FROM browser_action_event ORDER BY created_at DESC, sequence DESC LIMIT ?').all(boundedLimit)

      return rows.map(row => fromSql(row as Record<string, unknown>)).reverse()
    } catch {
      this.#degraded = true

      return []
    }
  }

  deleteSession(sessionId: string): boolean {
    if (!this.open() || !this.#db) {return false}

    try {
      this.#db.prepare('DELETE FROM browser_action_event WHERE session_id = ?').run(boundedId(sessionId, true))
      this.#destructiveMaintenance()

      return true
    } catch { this.#degraded = true;

 return false }
  }

  clear(workspaceId?: string): boolean {
    if (!this.open() || !this.#db) {return false}

    try {
      if (workspaceId) {this.#db.prepare('DELETE FROM browser_action_event WHERE workspace_id = ?').run(boundedId(workspaceId, true))}
      else {this.#db.exec('DELETE FROM browser_action_event')}

      this.#destructiveMaintenance()

      return true
    } catch { this.#degraded = true;

 return false }
  }

  prune(): boolean {
    if (!this.#db && !this.open()) {return false}

    try {
      this.#db!.exec('BEGIN IMMEDIATE')
      this.#pruneInTransaction()
      this.#db!.exec('COMMIT')
      this.#destructiveMaintenance()

      return true
    } catch {
      try { this.#db?.exec('ROLLBACK') } catch { /* already rolled back */ }
      this.#degraded = true

      return false
    }
  }

  close(): void {
    if (this.#pruneTimer) {clearInterval(this.#pruneTimer)}
    this.#pruneTimer = null
    this.#db?.close()
    this.#db = null
  }

  #settleRestart(): void {
    const now = this.#now()
    this.#db!.prepare(`UPDATE browser_action_event SET phase='not_started', certainty='not_started', reason_code='app_restart', terminal_at=? WHERE phase IN ('proposed','awaiting_consent','admitted')`).run(now)
    this.#db!.prepare(`UPDATE browser_action_event SET phase='outcome_unknown', certainty='outcome_unknown', reason_code='app_restart', terminal_at=? WHERE phase='dispatched'`).run(now)
  }

  #pruneInTransaction(): void {
    const cutoff = this.#now() - MAX_AGE_MS
    this.#db!.prepare('DELETE FROM browser_action_event WHERE created_at < ?').run(cutoff)
    const terminal = "phase IN ('completed','failed','late_frame_rejected','not_started','outcome_unknown')"
    const excess = `(SELECT CASE WHEN COUNT(*) > ${MAX_ROWS} THEN COUNT(*) - ${MAX_ROWS} ELSE 0 END FROM browser_action_event)`

    this.#db!.exec(`DELETE FROM browser_action_event WHERE event_id IN (
      SELECT event_id FROM browser_action_event WHERE ${terminal} ORDER BY created_at ASC, sequence ASC LIMIT ${excess}
    )`)
    this.#db!.exec(`UPDATE browser_action_event SET
      phase=CASE WHEN phase='dispatched' THEN 'outcome_unknown' ELSE 'not_started' END,
      certainty=CASE WHEN phase='dispatched' THEN 'outcome_unknown' ELSE 'not_started' END,
      reason_code='app_restart', terminal_at=${Math.trunc(this.#now())}
      WHERE event_id IN (
        SELECT event_id FROM browser_action_event WHERE NOT (${terminal}) ORDER BY created_at ASC, sequence ASC LIMIT ${excess}
      )`)
    this.#db!.prepare(`DELETE FROM browser_action_event WHERE event_id IN (
      SELECT event_id FROM browser_action_event WHERE ${terminal} ORDER BY created_at ASC, sequence ASC LIMIT ${excess}
    )`).run()
  }

  #destructiveMaintenance(): void {
    this.#db!.exec('PRAGMA wal_checkpoint(TRUNCATE)')
  }
}

export class BrowserActivityRepositoryManager {
  #repositories = new Map<string, BrowserActivityRepository>()
  constructor(private readonly userData: string) {}
  forProfile(profile: string): BrowserActivityRepository {
    const scope = browserProfileScope(profile)
    let repository = this.#repositories.get(scope)

    if (!repository) {
      repository = new BrowserActivityRepository(this.userData, profile)
      this.#repositories.set(scope, repository)
    }

    return repository
  }
  closeProfile(profile: string): void {
    const scope = browserProfileScope(profile)
    this.#repositories.get(scope)?.close()
    this.#repositories.delete(scope)
  }
  close(): void { for (const repository of this.#repositories.values()) {repository.close();} this.#repositories.clear() }
}
