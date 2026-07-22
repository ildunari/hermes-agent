import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const MAX_FILES = 20
const MAX_FILE_BYTES = 64 * 1024 * 1024
const MAX_TOTAL_BYTES = 256 * 1024 * 1024
const MAX_CHUNK_BYTES = 1024 * 1024
const MAX_ACTIVE_PER_CONNECTION = 2
const TRANSFER_TTL_MS = 5 * 60_000
const VERIFIED_TTL_MS = 10 * 60_000
const ASSIGNED_TTL_MS = 30 * 60_000
const CLEAR_TIMEOUT_MS = 5_000
const MAX_CLEANUP_ATTEMPTS = 3
const MAX_CLEAR_RETRIES_PER_SWEEP = 4
const CLEAR_RETRY_TTL_MS = 30_000
const ID_RE = /^[A-Za-z0-9_-]{43}$/
const SHA256_RE = /^[a-f0-9]{64}$/

export type BrowserUploadStagingErrorCode =
  | 'UPLOAD_EXPIRED'
  | 'UPLOAD_SOURCE_MUTATED'
  | 'UPLOAD_TOO_LARGE'
  | 'UPLOAD_TRANSFER_FAILED'

export class BrowserUploadStagingError extends Error {
  constructor(readonly code: BrowserUploadStagingErrorCode) {
    super(code)
  }
}

export interface BrowserUploadStagingBinding {
  authenticatedPrincipal: string
  backendNodeId: string
  bindingGeneration: string
  browserSid: string
  browserTransportId: string
  capabilityGeneration: string
  chooserId: string
  chooserMode: 'selectMultiple' | 'selectSingle'
  connectionId: string
  documentGeneration: string
  formFingerprint: string
  frameId: string
  guestGeneration: string
  origin: string
  profile: string
  sourceRecordRevision: string
  tabId: string
  tabIncarnation: string
  taskGeneration: string
  taskId: string
}

export interface BrowserUploadStageSource {
  bytes: AsyncIterable<Uint8Array>
  displayName: string
  mimeType: string
  sha256: string
  size: number
}

export interface BrowserUploadStagedFile {
  displayName: string
  mimeType: string
  sha256: string
  size: number
}

export interface BrowserUploadStagedHandle {
  expiresAt: number
  files: readonly BrowserUploadStagedFile[]
  handle: string
}

interface StagedRecord extends BrowserUploadStagedHandle {
  assigned: boolean
  binding: Readonly<BrowserUploadStagingBinding>
  directory: string
  identities: readonly StagedFileIdentity[]
  paths: readonly string[]
}

interface StagedFileIdentity {
  device: number
  inode: number
}

interface InFlightRecord {
  aborted: boolean
  abortController: AbortController
  binding: Readonly<BrowserUploadStagingBinding>
  deadline: number
  directory: string
  handle: string
  resolveSettled: () => void
  settled: Promise<void>
}

interface PendingAssignedClear {
  attempts: number
  deadline: number
  record: StagedRecord
}

interface BrowserUploadStagingDeps {
  clearAssignedInput?: (
    handle: string,
    binding: Readonly<BrowserUploadStagingBinding>
  ) => Promise<void> | void
  clock?: () => number
  randomBytes?: (size: number) => { toString(encoding: 'base64url'): string }
  root: string
}

function validText(value: unknown, max = 512): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= max &&
    Array.from(value).every(character => {
      const code = character.charCodeAt(0)

      return code > 31 && code !== 127
    })
}

function validBinding(value: BrowserUploadStagingBinding): boolean {
  if (!value || typeof value !== 'object') {return false}

  const keys = Object.keys(value).sort()

  const expected = [
    'authenticatedPrincipal', 'backendNodeId', 'bindingGeneration', 'browserSid',
    'browserTransportId', 'capabilityGeneration', 'chooserId', 'chooserMode',
    'connectionId', 'documentGeneration', 'formFingerprint', 'frameId',
    'guestGeneration', 'origin', 'profile', 'sourceRecordRevision', 'tabId',
    'tabIncarnation', 'taskGeneration', 'taskId'
  ].sort()

  return keys.length === expected.length && keys.every((key, index) => key === expected[index]) &&
    expected.every(key => validText(
      value[key as keyof BrowserUploadStagingBinding],
      key === 'sourceRecordRevision' ? 1024 : 512
    )) &&
    (value.chooserMode === 'selectSingle' || value.chooserMode === 'selectMultiple')
}

function exactBinding(left: BrowserUploadStagingBinding, right: BrowserUploadStagingBinding) {
  return validBinding(left) && validBinding(right) &&
    (Object.keys(left) as (keyof BrowserUploadStagingBinding)[]).every(key => left[key] === right[key])
}

function safeLeafName(value: string, index: number): string {
  const normalized = path.basename(value).normalize('NFKC')

  const cleaned = Array.from(normalized, character => {
    const code = character.codePointAt(0) ?? 0
    const control = code <= 31 || (code >= 127 && code <= 159) || code === 0x2028 || code === 0x2029

    return control || character === '/' || character === '\\' || character === ':' ? '_' : character
  }).join('').trim()

  const fallback = `upload-${index + 1}`
  const candidate = cleaned && cleaned !== '.' && cleaned !== '..' ? cleaned : fallback
  const extension = path.extname(candidate).slice(0, 32)
  let stem = path.basename(candidate, extension) || fallback

  while (Buffer.byteLength(`${stem}${extension}`) > 220) {stem = stem.slice(0, -1)}

  return `${stem || fallback}${extension}`
}

function validSource(source: BrowserUploadStageSource) {
  return validText(source.displayName, 1024) && validText(source.mimeType, 256) &&
    typeof source.sha256 === 'string' && SHA256_RE.test(source.sha256) &&
    Number.isSafeInteger(source.size) && source.size >= 0 && source.size <= MAX_FILE_BYTES
}

function rebaseUnderTrustedTemp(value: string): { root: string; trustedBase: string } {
  const rawBase = path.resolve(os.tmpdir())
  const rawRoot = path.resolve(value)
  const relative = path.relative(rawBase, rawRoot)

  if (!relative || relative.startsWith(`..${path.sep}`) || relative === '..' || path.isAbsolute(relative)) {
    throw new BrowserUploadStagingError('UPLOAD_TRANSFER_FAILED')
  }

  const trustedBase = fs.realpathSync.native(rawBase)

  return { root: path.join(trustedBase, relative), trustedBase }
}

/** Main-process-only owner of verified MacBook staging bytes. */
export class BrowserUploadStagingAuthority {
  readonly #activeConnections = new Map<string, number>()
  readonly #activeChoosers = new Set<string>()
  readonly #cleanupRetry = new Map<string, number>()
  readonly #clearAssignedInput: NonNullable<BrowserUploadStagingDeps['clearAssignedInput']>
  readonly #clock: () => number
  readonly #inFlight = new Map<string, InFlightRecord>()
  readonly #pendingAssignedClear = new Map<string, PendingAssignedClear>()
  readonly #randomBytes: NonNullable<BrowserUploadStagingDeps['randomBytes']>
  readonly #records = new Map<string, StagedRecord>()
  readonly #root: string
  readonly #trustedBase: string
  #initialized = false
  #assignedClearFailures = 0
  #ownedDirectoryIdentities: readonly { device: number; inode: number; path: string }[] = []
  #sweepPromise: Promise<number> | null = null
  #sweepTimer: ReturnType<typeof setInterval> | null = null
  #terminalCleanupFailures = 0

  constructor(deps: BrowserUploadStagingDeps) {
    const rebased = rebaseUnderTrustedTemp(deps.root)

    this.#root = rebased.root
    this.#trustedBase = rebased.trustedBase
    this.#clock = deps.clock ?? Date.now
    this.#randomBytes = deps.randomBytes ?? crypto.randomBytes
    this.#clearAssignedInput = deps.clearAssignedInput ?? (() => undefined)
  }

  /** Must complete before any browser surface is made available. */
  async initialize(): Promise<void> {
    const relative = path.relative(this.#trustedBase, this.#root)

    if (!relative || relative.startsWith(`..${path.sep}`) || relative === '..' || path.isAbsolute(relative)) {
      throw new BrowserUploadStagingError('UPLOAD_TRANSFER_FAILED')
    }

    const identities: { device: number; inode: number; path: string }[] = []
    let cursor = this.#trustedBase

    for (const component of relative.split(path.sep)) {
      cursor = path.join(cursor, component)

      try {
        await fs.promises.mkdir(cursor, { mode: 0o700 })
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'EEXIST') {throw error}
      }

      const info = await fs.promises.lstat(cursor)

      if (info.isSymbolicLink() || !info.isDirectory()) {
        throw new BrowserUploadStagingError('UPLOAD_TRANSFER_FAILED')
      }

      await fs.promises.chmod(cursor, 0o700)
      identities.push({ device: info.dev, inode: info.ino, path: cursor })
    }

    this.#ownedDirectoryIdentities = Object.freeze(identities)
    this.#verifyRoot()

    for (const entry of await fs.promises.readdir(this.#root)) {
      this.#verifyRoot()
      await fs.promises.rm(path.join(this.#root, entry), { force: true, recursive: true })
    }

    this.#verifyRoot()
    this.#initialized = true
    this.startPeriodicSweep()
  }

  startPeriodicSweep(intervalMs = 30_000) {
    if (!this.#initialized || this.#sweepTimer || !Number.isSafeInteger(intervalMs) || intervalMs < 1) {return false}
    this.#sweepTimer = setInterval(() => {
      void this.sweep().catch(() => undefined)
    }, intervalMs)
    this.#sweepTimer.unref()

    return true
  }

  stopPeriodicSweep() {
    if (this.#sweepTimer) {clearInterval(this.#sweepTimer)}
    this.#sweepTimer = null
  }

  async stage(
    binding: BrowserUploadStagingBinding,
    sources: readonly BrowserUploadStageSource[]
  ): Promise<BrowserUploadStagedHandle> {
    if (!this.#initialized || !validBinding(binding)) {throw new BrowserUploadStagingError('UPLOAD_TRANSFER_FAILED')}

    if (sources.length === 0 || sources.length > MAX_FILES || sources.some(source => !validSource(source))) {
      throw new BrowserUploadStagingError('UPLOAD_TOO_LARGE')
    }

    if (binding.chooserMode === 'selectSingle' && sources.length !== 1) {
      throw new BrowserUploadStagingError('UPLOAD_TRANSFER_FAILED')
    }

    const total = sources.reduce((sum, source) => sum + source.size, 0)

    if (!Number.isSafeInteger(total) || total > MAX_TOTAL_BYTES) {throw new BrowserUploadStagingError('UPLOAD_TOO_LARGE')}

    this.#collectExpired()
    this.#retryDirectoryCleanup()
    this.#verifyRoot()
    const chooserKey = `${binding.connectionId}\0${binding.chooserId}`
    const activeForConnection = this.#activeConnections.get(binding.connectionId) ?? 0

    if (activeForConnection >= MAX_ACTIVE_PER_CONNECTION || this.#activeChoosers.has(chooserKey)) {
      throw new BrowserUploadStagingError('UPLOAD_TRANSFER_FAILED')
    }

    const handle = this.#randomBytes(32).toString('base64url')

    if (!ID_RE.test(handle) || this.#records.has(handle) || this.#inFlight.has(handle) ||
      this.#pendingAssignedClear.has(handle)) {
      throw new BrowserUploadStagingError('UPLOAD_TRANSFER_FAILED')
    }

    const directory = path.join(this.#root, handle)

    let resolveSettled = () => undefined
    const settled = new Promise<void>(resolve => {resolveSettled = resolve})
    const inFlight: InFlightRecord = {
      aborted: false,
      abortController: new AbortController(),
      binding: Object.freeze({ ...binding }),
      deadline: this.#clock() + TRANSFER_TTL_MS,
      directory,
      handle,
      resolveSettled,
      settled
    }

    this.#inFlight.set(handle, inFlight)
    this.#activeConnections.set(binding.connectionId, activeForConnection + 1)
    this.#activeChoosers.add(chooserKey)

    try {
      await fs.promises.mkdir(directory, { mode: 0o700 })
      await fs.promises.chmod(directory, 0o700)
      this.#verifyRoot()
      const stagedPaths: string[] = []
      const stagedIdentities: StagedFileIdentity[] = []
      const files: BrowserUploadStagedFile[] = []
      const usedNames = new Set<string>()

      for (const [index, source] of sources.entries()) {
        this.#assertTransferLive(inFlight)
        let displayName = safeLeafName(source.displayName, index)
        const extension = path.extname(displayName)
        const stem = path.basename(displayName, extension)
        let suffix = 1

        while (usedNames.has(displayName.normalize('NFKC').toLocaleLowerCase('en-US'))) {
          displayName = `${stem}-${++suffix}${extension}`
        }

        usedNames.add(displayName.normalize('NFKC').toLocaleLowerCase('en-US'))

        const partial = path.join(directory, `${index}.partial`)

        const file = await fs.promises.open(
          partial,
          fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY | (fs.constants.O_NOFOLLOW ?? 0),
          0o600
        )

        const digest = crypto.createHash('sha256')
        let count = 0

        const iterator = source.bytes[Symbol.asyncIterator]()

        try {

          while (true) {
            const next = await this.#raceTransfer(iterator.next(), inFlight)

            if (next.done) {break}
            const raw = next.value
            this.#assertTransferLive(inFlight)
            const chunk = Buffer.from(raw.buffer, raw.byteOffset, raw.byteLength)

            if (chunk.byteLength === 0) {continue}

            if (chunk.byteLength > MAX_CHUNK_BYTES || count + chunk.byteLength > source.size) {
              throw new BrowserUploadStagingError('UPLOAD_SOURCE_MUTATED')
            }

            let offset = 0

            while (offset < chunk.byteLength) {
              this.#assertTransferLive(inFlight)
              const { bytesWritten } = await this.#raceTransfer(
                file.write(chunk, offset, chunk.byteLength - offset),
                inFlight
              )

              if (bytesWritten <= 0) {throw new BrowserUploadStagingError('UPLOAD_TRANSFER_FAILED')}
              offset += bytesWritten
            }

            digest.update(chunk)
            count += chunk.byteLength
          }

          await this.#raceTransfer(file.sync(), inFlight)
        } finally {
          if (iterator.return) {
            const returned = Promise.resolve(iterator.return())
            if (inFlight.aborted) {returned.catch(() => undefined)}
            else {await this.#raceTransfer(returned, inFlight).catch(() => undefined)}
          }
          await this.#raceTransfer(file.close(), inFlight).catch(() => undefined)
        }

        this.#assertTransferLive(inFlight)

        if (count !== source.size || digest.digest('hex') !== source.sha256) {
          throw new BrowserUploadStagingError('UPLOAD_SOURCE_MUTATED')
        }

        const installed = await this.#installNoClobber(partial, directory, displayName)
        displayName = installed.displayName
        const finalPath = installed.path
        await fs.promises.chmod(finalPath, 0o600)
        const finalInfo = await fs.promises.lstat(finalPath)
        if (!finalInfo.isFile() || finalInfo.isSymbolicLink() || finalInfo.size !== source.size ||
          (process.platform !== 'win32' && (finalInfo.mode & 0o777) !== 0o600)) {
          throw new BrowserUploadStagingError('UPLOAD_SOURCE_MUTATED')
        }
        stagedPaths.push(finalPath)
        stagedIdentities.push(Object.freeze({ device: finalInfo.dev, inode: finalInfo.ino }))
        files.push({ displayName, mimeType: source.mimeType, sha256: source.sha256, size: source.size })
      }

      this.#assertTransferLive(inFlight)

      const record: StagedRecord = Object.freeze({
        assigned: false,
        binding: inFlight.binding,
        directory,
        expiresAt: this.#clock() + VERIFIED_TTL_MS,
        files: Object.freeze(files.map(file => Object.freeze(file))),
        handle,
        identities: Object.freeze(stagedIdentities),
        paths: Object.freeze(stagedPaths)
      })

      this.#records.set(handle, record)

      return { expiresAt: record.expiresAt, files: record.files, handle }
    } catch (error) {
      this.#removeDirectory(directory)

      if (error instanceof BrowserUploadStagingError) {throw error}
      throw new BrowserUploadStagingError('UPLOAD_TRANSFER_FAILED')
    } finally {
      this.#inFlight.delete(handle)
      this.#releaseConcurrency(binding, chooserKey)
      inFlight.resolveSettled()
    }
  }

  /** Atomically consumes one verified handle. Paths stay inside Electron main. */
  async consume(handle: string, binding: BrowserUploadStagingBinding): Promise<readonly string[]> {
    this.#collectExpired()
    this.#retryDirectoryCleanup()
    const record = this.#records.get(handle)

    if (!record || record.assigned || record.expiresAt <= this.#clock()) {
      throw new BrowserUploadStagingError('UPLOAD_EXPIRED')
    }

    if (!exactBinding(record.binding, binding)) {
      await this.#deleteRecord(record)
      throw new BrowserUploadStagingError('UPLOAD_EXPIRED')
    }

    try {
      await this.#revalidateStagedFiles(record)
    } catch {
      await this.#deleteRecord(record)
      throw new BrowserUploadStagingError('UPLOAD_SOURCE_MUTATED')
    }

    const consumed: StagedRecord = Object.freeze({
      ...record,
      assigned: true,
      expiresAt: this.#clock() + ASSIGNED_TTL_MS
    })

    this.#records.set(handle, consumed)

    return consumed.paths
  }

  async retire(handle: string): Promise<boolean> {
    const record = this.#records.get(handle)

    if (!record) {return false}
    await this.#deleteRecord(record)
    const pending = this.#pendingAssignedClear.get(handle)
    if (pending) {await this.#retryAssignedClear(pending)}

    return true
  }

  async revokeWhere(predicate: (binding: Readonly<BrowserUploadStagingBinding>) => boolean): Promise<number> {
    let count = 0

    for (const transfer of this.#inFlight.values()) {
      if (predicate(transfer.binding) && !transfer.aborted) {
        this.#abortTransfer(transfer)
        count += 1
      }
    }

    for (const record of [...this.#records.values()]) {
      if (predicate(record.binding)) {
        await this.#deleteRecord(record)
        const pending = this.#pendingAssignedClear.get(record.handle)
        if (pending) {await this.#retryAssignedClear(pending)}
        count += 1
      }
    }

    return count
  }

  /** Bounded quit barrier: revoke authority, clear assigned inputs, then unlink. */
  async shutdown(): Promise<void> {
    this.stopPeriodicSweep()
    await this.revokeWhere(() => true)
    const transfers = [...this.#inFlight.values()]
    await this.#withTimeout(Promise.all(transfers.map(transfer => transfer.settled)), CLEAR_TIMEOUT_MS)
    for (let attempt = 0; attempt < MAX_CLEANUP_ATTEMPTS; attempt += 1) {
      await Promise.allSettled(
        [...this.#pendingAssignedClear.values()].map(entry => this.#retryAssignedClear(entry))
      )
      this.#retryDirectoryCleanup()
      if (this.#pendingAssignedClear.size === 0 && this.#cleanupRetry.size === 0) {break}
    }
    if (this.#pendingAssignedClear.size > 0 || this.#cleanupRetry.size > 0 ||
      this.#assignedClearFailures > 0 || this.#terminalCleanupFailures > 0) {
      throw new BrowserUploadStagingError('UPLOAD_TRANSFER_FAILED')
    }
  }

  async sweep(): Promise<number> {
    if (this.#sweepPromise) {return this.#sweepPromise}

    const running = this.#runSweep()
    this.#sweepPromise = running

    try {
      return await running
    } finally {
      if (this.#sweepPromise === running) {this.#sweepPromise = null}
    }
  }

  cleanupFailures(): number {
    return this.#cleanupRetry.size + this.#pendingAssignedClear.size +
      this.#assignedClearFailures + this.#terminalCleanupFailures
  }

  assignedClearFailures(): number {
    return this.#assignedClearFailures
  }

  async #runSweep(): Promise<number> {
    const count = this.#collectExpired()
    const pending = [...this.#pendingAssignedClear.values()].slice(0, MAX_CLEAR_RETRIES_PER_SWEEP)

    await Promise.allSettled(pending.map(entry => this.#retryAssignedClear(entry)))
    this.#retryDirectoryCleanup()

    return count
  }

  #collectExpired(): number {
    const now = this.#clock()
    let count = 0

    for (const transfer of this.#inFlight.values()) {
      if (transfer.deadline <= now && !transfer.aborted) {
        this.#abortTransfer(transfer)
        count += 1
      }
    }

    for (const record of [...this.#records.values()]) {
      if (record.expiresAt <= now) {
        this.#deleteRecord(record)
        count += 1
      }
    }

    return count
  }

  #assertTransferLive(transfer: InFlightRecord) {
    if (transfer.aborted || transfer.deadline <= this.#clock() || this.#inFlight.get(transfer.handle) !== transfer) {
      this.#abortTransfer(transfer)
      throw new BrowserUploadStagingError('UPLOAD_EXPIRED')
    }

    this.#verifyRoot()
  }

  #deleteRecord(record: StagedRecord) {
    this.#records.delete(record.handle)

    if (record.assigned) {
      if (!this.#pendingAssignedClear.has(record.handle)) {
        this.#pendingAssignedClear.set(record.handle, {
          attempts: 0,
          deadline: this.#clock() + CLEAR_RETRY_TTL_MS,
          record
        })
      }

      return
    }

    this.#removeDirectory(record.directory)
  }

  #abortTransfer(transfer: InFlightRecord) {
    transfer.aborted = true
    transfer.abortController.abort()
  }

  async #raceTransfer<T>(operation: Promise<T>, transfer: InFlightRecord): Promise<T> {
    operation.catch(() => undefined)
    this.#assertTransferLive(transfer)
    const remaining = Math.max(0, transfer.deadline - this.#clock())
    let timer: ReturnType<typeof setTimeout> | undefined
    let abortListener: (() => void) | undefined

    const cancelled = new Promise<never>((_resolve, reject) => {
      const rejectExpired = () => reject(new BrowserUploadStagingError('UPLOAD_EXPIRED'))

      abortListener = rejectExpired
      transfer.abortController.signal.addEventListener('abort', rejectExpired, { once: true })
      timer = setTimeout(() => {
        this.#abortTransfer(transfer)
        rejectExpired()
      }, remaining)
      timer.unref()
    })

    try {
      return await Promise.race([operation, cancelled])
    } finally {
      operation.catch(() => undefined)
      if (timer) {clearTimeout(timer)}
      if (abortListener) {transfer.abortController.signal.removeEventListener('abort', abortListener)}
    }
  }

  async #retryAssignedClear(entry: PendingAssignedClear) {
    const current = this.#pendingAssignedClear.get(entry.record.handle)

    if (current !== entry) {return}
    entry.attempts += 1

    try {
      await this.#withTimeout(
        Promise.resolve(this.#clearAssignedInput(entry.record.handle, entry.record.binding)),
        CLEAR_TIMEOUT_MS
      )
      this.#pendingAssignedClear.delete(entry.record.handle)
      this.#removeDirectory(entry.record.directory)
    } catch {
      if (entry.attempts >= MAX_CLEANUP_ATTEMPTS || entry.deadline <= this.#clock()) {
        this.#pendingAssignedClear.delete(entry.record.handle)
        this.#assignedClearFailures += 1
        this.#removeDirectory(entry.record.directory)
      }
    }
  }

  async #withTimeout<T>(operation: Promise<T>, timeoutMs: number): Promise<T> {
    let timer: ReturnType<typeof setTimeout> | undefined
    const timeout = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => reject(new BrowserUploadStagingError('UPLOAD_TRANSFER_FAILED')), timeoutMs)
      timer.unref()
    })

    try {
      return await Promise.race([operation, timeout])
    } finally {
      operation.catch(() => undefined)
      if (timer) {clearTimeout(timer)}
    }
  }

  async #installNoClobber(partial: string, directory: string, requestedName: string) {
    const extension = path.extname(requestedName)
    const stem = path.basename(requestedName, extension)

    for (let suffix = 1; suffix <= MAX_FILES + 1; suffix += 1) {
      const displayName = suffix === 1 ? requestedName : `${stem}-${suffix}${extension}`
      const finalPath = path.join(directory, displayName)

      try {
        await fs.promises.link(partial, finalPath)
        await fs.promises.unlink(partial)

        return { displayName, path: finalPath }
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'EEXIST') {throw error}
      }
    }

    throw new BrowserUploadStagingError('UPLOAD_TRANSFER_FAILED')
  }

  async #revalidateStagedFiles(record: StagedRecord): Promise<void> {
    this.#verifyRoot()
    const directoryInfo = await fs.promises.lstat(record.directory)
    if (directoryInfo.isSymbolicLink() || !directoryInfo.isDirectory() ||
      (process.platform !== 'win32' && (directoryInfo.mode & 0o777) !== 0o700) ||
      record.paths.length !== record.files.length || record.identities.length !== record.files.length) {
      throw new BrowserUploadStagingError('UPLOAD_SOURCE_MUTATED')
    }

    for (const [index, filePath] of record.paths.entries()) {
      const expected = record.files[index]!
      const identity = record.identities[index]!
      if (path.dirname(filePath) !== record.directory) {
        throw new BrowserUploadStagingError('UPLOAD_SOURCE_MUTATED')
      }
      const handle = await fs.promises.open(
        filePath,
        fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0)
      )
      try {
        const before = await handle.stat()
        if (!before.isFile() || before.dev !== identity.device || before.ino !== identity.inode ||
          before.size !== expected.size || (process.platform !== 'win32' && (before.mode & 0o777) !== 0o600)) {
          throw new BrowserUploadStagingError('UPLOAD_SOURCE_MUTATED')
        }
        const digest = crypto.createHash('sha256')
        const buffer = Buffer.allocUnsafe(MAX_CHUNK_BYTES)
        let offset = 0
        while (offset < expected.size) {
          const { bytesRead } = await handle.read(buffer, 0, Math.min(buffer.length, expected.size - offset), offset)
          if (bytesRead <= 0) {throw new BrowserUploadStagingError('UPLOAD_SOURCE_MUTATED')}
          digest.update(buffer.subarray(0, bytesRead))
          offset += bytesRead
        }
        const trailing = Buffer.allocUnsafe(1)
        if ((await handle.read(trailing, 0, 1, offset)).bytesRead !== 0 || digest.digest('hex') !== expected.sha256) {
          throw new BrowserUploadStagingError('UPLOAD_SOURCE_MUTATED')
        }
        const after = await handle.stat()
        const pathname = await fs.promises.lstat(filePath)
        if (after.dev !== before.dev || after.ino !== before.ino || after.size !== before.size ||
          after.mtimeMs !== before.mtimeMs || after.ctimeMs !== before.ctimeMs ||
          pathname.isSymbolicLink() || !pathname.isFile() || pathname.dev !== identity.device ||
          pathname.ino !== identity.inode || pathname.size !== expected.size) {
          throw new BrowserUploadStagingError('UPLOAD_SOURCE_MUTATED')
        }
      } finally {
        await handle.close().catch(() => undefined)
      }
    }
    this.#verifyRoot()
  }

  #releaseConcurrency(binding: BrowserUploadStagingBinding, chooserKey: string) {
    const remaining = (this.#activeConnections.get(binding.connectionId) ?? 1) - 1

    if (remaining > 0) {this.#activeConnections.set(binding.connectionId, remaining)}
    else {this.#activeConnections.delete(binding.connectionId)}

    this.#activeChoosers.delete(chooserKey)
  }

  #retryDirectoryCleanup() {
    for (const directory of [...this.#cleanupRetry.keys()]) {this.#removeDirectory(directory)}
  }

  #removeDirectory(directory: string): boolean {
    try {
      fs.rmSync(directory, { force: true, recursive: true })
      this.#cleanupRetry.delete(directory)

      return true
    } catch {
      const attempts = (this.#cleanupRetry.get(directory) ?? 0) + 1

      if (attempts >= MAX_CLEANUP_ATTEMPTS) {
        this.#cleanupRetry.delete(directory)
        this.#terminalCleanupFailures += 1
      } else {
        this.#cleanupRetry.set(directory, attempts)
      }

      return false
    }
  }

  #verifyRoot() {
    try {
      if (this.#ownedDirectoryIdentities.length === 0) {
        throw new BrowserUploadStagingError('UPLOAD_TRANSFER_FAILED')
      }

      for (const identity of this.#ownedDirectoryIdentities) {
        const info = fs.lstatSync(identity.path)

        if (info.isSymbolicLink() || !info.isDirectory() ||
          info.dev !== identity.device || info.ino !== identity.inode) {
          throw new BrowserUploadStagingError('UPLOAD_TRANSFER_FAILED')
        }
      }
    } catch (error) {
      if (error instanceof BrowserUploadStagingError) {throw error}
      throw new BrowserUploadStagingError('UPLOAD_TRANSFER_FAILED')
    }
  }
}
