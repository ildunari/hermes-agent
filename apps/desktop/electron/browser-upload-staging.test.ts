import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  type BrowserUploadStageSource,
  BrowserUploadStagingAuthority,
  type BrowserUploadStagingBinding,
  BrowserUploadStagingError
} from './browser-upload-staging'

const roots: string[] = []
const authorities: BrowserUploadStagingAuthority[] = []
const HANDLE = 'h'.repeat(43)

function binding(overrides: Partial<BrowserUploadStagingBinding> = {}): BrowserUploadStagingBinding {
  return {
    authenticatedPrincipal: 'principal-1',
    backendNodeId: 'node-1',
    bindingGeneration: 'binding-1',
    browserSid: 'browser-1',
    browserTransportId: 'transport-1',
    capabilityGeneration: 'capability-1',
    chooserId: 'chooser-1',
    chooserMode: 'selectSingle',
    connectionId: 'connection-1',
    documentGeneration: 'document-1',
    formFingerprint: 'form-1',
    frameId: 'frame-1',
    guestGeneration: 'guest-1',
    origin: 'https://upload.example',
    profile: 'default',
    sourceRecordRevision: 'source-revision-1',
    tabId: 'tab-1',
    tabIncarnation: 'incarnation-1',
    taskGeneration: 'task-generation-1',
    taskId: 'task-1',
    ...overrides
  }
}

async function * chunks(...values: (Buffer | string)[]) {
  for (const value of values) {yield typeof value === 'string' ? Buffer.from(value) : value}
}

function source(name: string, value: string, overrides: Partial<BrowserUploadStageSource> = {}): BrowserUploadStageSource {
  const bytes = Buffer.from(value)

  return {
    bytes: chunks(bytes),
    displayName: name,
    mimeType: 'text/plain',
    sha256: crypto.createHash('sha256').update(bytes).digest('hex'),
    size: bytes.byteLength,
    ...overrides
  }
}

function fixture(
  now = { value: 1_000 },
  deps: { clearAssignedInput?: (handle: string, binding: Readonly<BrowserUploadStagingBinding>) => Promise<void> | void } = {}
) {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-upload-staging-test-'))
  roots.push(parent)
  const root = path.join(parent, 'hermes-browser-upload-v1', 'desktop-instance')

  const authority = new BrowserUploadStagingAuthority({
    ...deps,
    clock: () => now.value,
    randomBytes: () => ({ toString: () => HANDLE }),
    root
  })
  authorities.push(authority)

  return { authority, now, root }
}

afterEach(() => {
  for (const authority of authorities.splice(0)) {authority.stopPeriodicSweep()}
  for (const root of roots.splice(0)) {fs.rmSync(root, { force: true, recursive: true })}
  vi.restoreAllMocks()
})

describe('BrowserUploadStagingAuthority', () => {
  it('sweeps crash residue before becoming available and creates an owner-only root', async () => {
    const { authority, root } = fixture()
    fs.mkdirSync(path.join(root, 'stale-handle'), { recursive: true })
    fs.writeFileSync(path.join(root, 'stale-handle', 'bytes'), 'secret')

    await authority.initialize()

    expect(fs.readdirSync(root)).toEqual([])

    if (process.platform !== 'win32') {
      expect(fs.statSync(root).mode & 0o777).toBe(0o700)
    }
  })

  it('stages exact bytes, sanitizes names, preserves extension, and consumes once', async () => {
    const { authority, root } = fixture()
    await authority.initialize()
    const staged = await authority.stage(binding(), [source('../bad:name.txt', 'hello')])

    expect(staged.handle).toBe(HANDLE)
    expect(staged.files).toEqual([
      expect.objectContaining({ displayName: 'bad_name.txt', mimeType: 'text/plain', size: 5 })
    ])
    const paths = await authority.consume(HANDLE, binding())
    expect(paths).toHaveLength(1)
    expect(fs.readFileSync(paths[0]!, 'utf8')).toBe('hello')
    expect(path.dirname(paths[0]!)).toBe(path.join(fs.realpathSync.native(path.dirname(root)), path.basename(root), HANDLE))

    if (process.platform !== 'win32') {
      expect(fs.statSync(paths[0]!).mode & 0o777).toBe(0o600)
    }

    await expect(authority.consume(HANDLE, binding())).rejects.toThrowError('UPLOAD_EXPIRED')
  })

  it('deletes every staged member atomically when a later digest mismatches', async () => {
    const { authority, root } = fixture()
    await authority.initialize()

    await expect(authority.stage(binding({ chooserMode: 'selectMultiple' }), [
      source('one.txt', 'one'),
      source('two.txt', 'two', { sha256: '0'.repeat(64) })
    ])).rejects.toMatchObject({ code: 'UPLOAD_SOURCE_MUTATED' })
    expect(fs.readdirSync(root)).toEqual([])
  })

  it('rejects oversized declarations and application chunks before retaining bytes', async () => {
    const { authority, root } = fixture()
    await authority.initialize()
    await expect(authority.stage(binding(), [
      source('huge.bin', '', { size: 64 * 1024 * 1024 + 1 })
    ])).rejects.toMatchObject({ code: 'UPLOAD_TOO_LARGE' })

    const oversizedChunk = Buffer.alloc(1024 * 1024 + 1)
    await expect(authority.stage(binding(), [source('chunk.bin', '', {
      bytes: chunks(oversizedChunk),
      sha256: crypto.createHash('sha256').update(oversizedChunk).digest('hex'),
      size: oversizedChunk.byteLength
    })])).rejects.toMatchObject({ code: 'UPLOAD_SOURCE_MUTATED' })
    expect(fs.readdirSync(root)).toEqual([])
  })

  it('revokes and unlinks on exact-binding mismatch', async () => {
    const { authority, root } = fixture()
    await authority.initialize()
    await authority.stage(binding(), [source('one.txt', 'one')])

    await expect(authority.consume(HANDLE, binding({ documentGeneration: 'document-2' })))
      .rejects.toThrowError('UPLOAD_EXPIRED')
    expect(fs.readdirSync(root)).toEqual([])
    await expect(authority.consume(HANDLE, binding())).rejects.toThrowError('UPLOAD_EXPIRED')
  })

  it('re-stats and re-hashes staged bytes immediately before consumption', async () => {
    const { authority, root } = fixture()
    await authority.initialize()
    await authority.stage(binding(), [source('one.txt', 'one')])
    const stagedPath = path.join(root, HANDLE, 'one.txt')
    fs.writeFileSync(stagedPath, 'two')

    await expect(authority.consume(HANDLE, binding())).rejects.toMatchObject({
      code: 'UPLOAD_SOURCE_MUTATED'
    })
    expect(fs.existsSync(stagedPath)).toBe(false)
  })

  it('expires unconsumed and assigned records at their separate hard deadlines', async () => {
    const first = fixture()
    await first.authority.initialize()
    await first.authority.stage(binding(), [source('one.txt', 'one')])
    first.now.value += 10 * 60_000
    expect(await first.authority.sweep()).toBe(1)
    expect(fs.readdirSync(first.root)).toEqual([])

    const second = fixture()
    await second.authority.initialize()
    await second.authority.stage(binding(), [source('one.txt', 'one')])
    await second.authority.consume(HANDLE, binding())
    second.now.value += 30 * 60_000
    expect(await second.authority.sweep()).toBe(1)
    expect(fs.readdirSync(second.root)).toEqual([])
  })

  it('revokes all records matching a retired lifecycle scope', async () => {
    const { authority, root } = fixture()
    await authority.initialize()
    await authority.stage(binding(), [source('one.txt', 'one')])

    expect(await authority.revokeWhere(scope => scope.profile === 'default' && scope.tabId === 'tab-1')).toBe(1)
    expect(fs.readdirSync(root)).toEqual([])
  })

  it('fences an in-flight transfer before it can resurrect a revoked chooser', async () => {
    const { authority, root } = fixture()
    await authority.initialize()
    let release!: () => void
    const waiting = new Promise<void>(resolve => {release = resolve})

    const bytes = (async function * () {
      yield Buffer.from('one')
      await waiting
      yield Buffer.from('two')
    })()

    const value = Buffer.from('onetwo')

    const staging = authority.stage(binding(), [{
      bytes,
      displayName: 'one.txt',
      mimeType: 'text/plain',
      sha256: crypto.createHash('sha256').update(value).digest('hex'),
      size: value.byteLength
    }])

    await new Promise(resolve => setTimeout(resolve, 10))

    expect(await authority.revokeWhere(scope => scope.chooserId === 'chooser-1')).toBe(1)
    release()
    await expect(staging).rejects.toMatchObject({ code: 'UPLOAD_EXPIRED' })
    expect(fs.readdirSync(root)).toEqual([])
  })

  it('keeps case-folded duplicate names as distinct atomic members', async () => {
    const { authority } = fixture()
    await authority.initialize()

    const staged = await authority.stage(binding({ chooserMode: 'selectMultiple' }), [
      source('same.txt', 'first'),
      source('SAME.txt', 'second')
    ])

    const paths = await authority.consume(staged.handle, binding({ chooserMode: 'selectMultiple' }))

    expect(paths.map(file => path.basename(file))).toEqual(['same.txt', 'SAME-2.txt'])
    expect(paths.map(file => fs.readFileSync(file, 'utf8'))).toEqual(['first', 'second'])
  })

  it('rejects a symlinked staging parent instead of traversing it during startup cleanup', async () => {
    if (process.platform === 'win32') {return}
    const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-upload-symlink-test-'))
    roots.push(parent)
    const target = path.join(parent, 'unrelated')
    const stagingParent = path.join(parent, 'hermes-browser-upload-v1')
    const nested = path.join(stagingParent, 'nested')
    fs.mkdirSync(stagingParent)
    fs.mkdirSync(target)
    fs.writeFileSync(path.join(target, 'keep'), 'keep')
    fs.symlinkSync(target, nested)
    const authority = new BrowserUploadStagingAuthority({ root: path.join(nested, 'instance') })

    await expect(authority.initialize()).rejects.toMatchObject({ code: 'UPLOAD_TRANSFER_FAILED' })
    expect(fs.readFileSync(path.join(target, 'keep'), 'utf8')).toBe('keep')
  })

  it('uses filesystem no-clobber installation when Unicode-equivalent names collide', async () => {
    const { authority } = fixture()
    await authority.initialize()
    const realLink = fs.promises.link.bind(fs.promises)
    const collision = Object.assign(new Error('collision'), { code: 'EEXIST' })

    vi.spyOn(fs.promises, 'link')
      .mockRejectedValueOnce(collision)
      .mockImplementation(realLink)

    const staged = await authority.stage(binding(), [source('σ.txt', 'value')])
    const paths = await authority.consume(staged.handle, binding())

    expect(staged.files[0]?.displayName).toBe('σ-2.txt')
    expect(path.basename(paths[0]!)).toBe('σ-2.txt')
    expect(fs.readFileSync(paths[0]!, 'utf8')).toBe('value')
  })

  it('releases chooser concurrency when a non-cooperative iterator never returns', async () => {
    const { authority } = fixture()
    await authority.initialize()
    const never = new Promise<IteratorResult<Uint8Array>>(() => undefined)
    const bytes: AsyncIterable<Uint8Array> = {
      [Symbol.asyncIterator]: () => ({ next: () => never })
    }
    const staging = authority.stage(binding(), [{
      bytes,
      displayName: 'blocked.txt',
      mimeType: 'text/plain',
      sha256: crypto.createHash('sha256').update('x').digest('hex'),
      size: 1
    }])

    await new Promise(resolve => setTimeout(resolve, 10))
    expect(await authority.revokeWhere(scope => scope.chooserId === 'chooser-1')).toBe(1)
    await expect(staging).rejects.toMatchObject({ code: 'UPLOAD_EXPIRED' })

    const replacement = await authority.stage(binding(), [source('replacement.txt', 'ok')])
    expect(replacement.files[0]?.displayName).toBe('replacement.txt')
  })

  it('clears assigned input before deletion and retries bounded async clear failures', async () => {
    const now = { value: 1_000 }
    const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-upload-clear-test-'))
    roots.push(parent)
    const root = path.join(parent, 'hermes-browser-upload-v1', 'desktop-instance')
    let clearAttempts = 0
    let assignedPath = ''
    const authority = new BrowserUploadStagingAuthority({
      clearAssignedInput: async () => {
        clearAttempts += 1
        expect(fs.existsSync(assignedPath)).toBe(true)
        if (clearAttempts === 1) {throw new Error('transient clear failure')}
      },
      clock: () => now.value,
      randomBytes: () => ({ toString: () => HANDLE }),
      root
    })
    authorities.push(authority)
    await authority.initialize()
    expect(authority.startPeriodicSweep()).toBe(false)
    await authority.stage(binding(), [source('assigned.txt', 'value')])
    const assignedPaths = await authority.consume(HANDLE, binding())
    assignedPath = assignedPaths[0] ?? ''

    expect(await authority.retire(HANDLE)).toBe(true)
    expect(clearAttempts).toBe(1)
    expect(authority.cleanupFailures()).toBe(1)
    expect(fs.existsSync(assignedPath)).toBe(true)

    await authority.sweep()
    expect(clearAttempts).toBe(2)
    expect(authority.cleanupFailures()).toBe(0)
    expect(fs.existsSync(assignedPath)).toBe(false)
  })

  it('terminally deletes assigned bytes after a permanently failing clear callback', async () => {
    const now = { value: 1_000 }
    const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-upload-clear-terminal-test-'))
    roots.push(parent)
    const root = path.join(parent, 'hermes-browser-upload-v1', 'desktop-instance')
    let clearAttempts = 0
    const authority = new BrowserUploadStagingAuthority({
      clearAssignedInput: async () => {
        clearAttempts += 1
        throw new Error('guest is gone')
      },
      clock: () => now.value,
      randomBytes: () => ({ toString: () => HANDLE }),
      root
    })
    authorities.push(authority)
    await authority.initialize()
    await authority.stage(binding(), [source('assigned.txt', 'secret')])
    const [assignedPath] = await authority.consume(HANDLE, binding())
    await authority.retire(HANDLE)

    expect(clearAttempts).toBe(1)
    await authority.sweep()
    await authority.sweep()

    expect(clearAttempts).toBe(3)
    expect(authority.assignedClearFailures()).toBe(1)
    expect(authority.cleanupFailures()).toBe(1)
    expect(fs.existsSync(assignedPath!)).toBe(false)

    await authority.sweep()
    expect(clearAttempts).toBe(3)
  })

  it('fails closed before startup sweep and for incomplete runtime bindings', async () => {
    const { authority } = fixture()
    await expect(authority.stage(binding(), [source('one.txt', 'one')]))
      .rejects.toBeInstanceOf(BrowserUploadStagingError)
    await authority.initialize()
    await expect(authority.stage({} as BrowserUploadStagingBinding, [source('one.txt', 'one')]))
      .rejects.toMatchObject({ code: 'UPLOAD_TRANSFER_FAILED' })
  })

  it('shutdown clears assigned inputs and unlinks staged bytes before resolving', async () => {
    const clearAssignedInput = vi.fn(async () => undefined)
    const { authority, root } = fixture(undefined, { clearAssignedInput })
    await authority.initialize()
    const staged = await authority.stage(binding(), [source('quit.txt', 'quit')])
    await authority.consume(staged.handle, binding())

    await expect(authority.shutdown()).resolves.toBeUndefined()
    expect(clearAssignedInput).toHaveBeenCalledTimes(1)
    expect(fs.readdirSync(root)).toEqual([])
  })

  it('lifecycle revocation immediately clears an assigned input before unlinking', async () => {
    const clearAssignedInput = vi.fn(async () => undefined)
    const { authority, root } = fixture(undefined, { clearAssignedInput })
    await authority.initialize()
    const staged = await authority.stage(binding(), [source('navigation.txt', 'secret')])
    await authority.consume(staged.handle, binding())

    await expect(authority.revokeWhere(scope => scope.documentGeneration === 'document-1')).resolves.toBe(1)
    expect(clearAssignedInput).toHaveBeenCalledTimes(1)
    expect(fs.readdirSync(root)).toEqual([])
  })

  it('shutdown reports a terminal assigned-input clear failure after bounded retries', async () => {
    const { authority, root } = fixture(undefined, { clearAssignedInput: async () => {throw new Error('clear failed')} })
    await authority.initialize()
    const staged = await authority.stage(binding(), [source('quit.txt', 'quit')])
    await authority.consume(staged.handle, binding())

    await expect(authority.shutdown()).rejects.toMatchObject({ code: 'UPLOAD_TRANSFER_FAILED' })
    expect(fs.readdirSync(root)).toEqual([])
  })

  it('stages all 20 selected records with their exact ordered server revisions', async () => {
    const { authority } = fixture()
    await authority.initialize()
    const revisions = Array.from({ length: 20 }, (_, index) => String(index).padStart(32, 'r')).join('.')
    const sources = Array.from({ length: 20 }, (_, index) => source(`file-${index}.txt`, String(index)))

    const staged = await authority.stage(binding({
      chooserMode: 'selectMultiple', sourceRecordRevision: revisions
    }), sources)
    expect(staged.files).toHaveLength(20)
  })
})
