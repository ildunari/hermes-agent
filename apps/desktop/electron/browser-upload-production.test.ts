import { describe, expect, it, vi } from 'vitest'

import {
  browserUploadDeliveryHeaders,
  type BrowserUploadImportDeps,
  importBrowserUpload,
  selectBrowserUploadCandidates
} from './browser-upload-production'

function candidates(count: number) {
  return Array.from({ length: count }, (_, index) => ({
    candidateId: String(index).padStart(32, 'a'), displayName: `file-${index}.txt`, mimeType: 'text/plain',
    size: index, sourceRecordRevision: String(index).padStart(32, 'r')
  }))
}

const scope = {
  connection_id: 'connection', transport_id: 'transport', browser_sid: 'sid', capability_generation: '2',
  task_id: 'task', task_generation: '7', tab_id: 'tab', tab_incarnation: 'incarnation', binding_generation: '3',
  document_generation: '4', frame_id: 'frame', origin: 'https://example.test', chooser_id: 'chooser',
  backend_node_id: 'node', form_fingerprint: 'form', chooser_mode: 'selectMultiple', source_session_id: 'session'
}

function chooser(signal: AbortSignal) {
  return {
    accept: '', backendNodeId: 4, chooserId: 'chooser', directory: false as const, documentGeneration: 4,
    formActionOrigin: 'https://example.test', formActionUrl: 'https://example.test/upload', formFingerprint: 'form',
    formLabel: 'Upload', formMethod: 'post' as const, frameId: 'frame', guestGeneration: 'guest', hostId: 1,
    inputLabel: 'File', inputName: 'file', mode: 'selectMultiple' as const, origin: 'https://example.test',
    profile: 'coding', signal, tabId: 'tab', taskGeneration: 7, taskId: 'task'
  }
}

function ticket(row: ReturnType<typeof candidates>[number], index = 0) {
  return {
    deliveryCredential: String(index).padStart(32, 'g'), displayName: row.displayName,
    mimeType: row.mimeType, opaqueRef: String(index).padStart(32, 'o'), recipient: 'principal',
    sha256: String(index).padStart(64, 'a'), size: row.size, sourceRecordRevision: row.sourceRecordRevision
  }
}

function importerDeps(
  rows: ReturnType<typeof candidates>,
  overrides: Partial<BrowserUploadImportDeps> = {}
): BrowserUploadImportDeps {
  return {
    assign: async () => 'completed',
    bytes: async function* () {yield new Uint8Array([1])},
    choose: async () => 0,
    consume: async () => rows.map((_, index) => `/tmp/${index}`),
    requestJson: async (requestPath, body) => {
      if (requestPath.endsWith('candidates')) {return { candidates: rows }}
      if (requestPath.endsWith('revoke')) {return { ok: true }}
      const index = rows.findIndex(row => row.candidateId === body.candidate_id)
      return ticket(rows[index], index)
    },
    retire: async () => true,
    scope,
    stage: async (_binding, sources) => ({
      expiresAt: Date.now() + 1_000,
      files: sources.map(source => ({
        displayName: source.displayName, mimeType: source.mimeType, sha256: source.sha256, size: source.size
      })),
      handle: 'handle'
    }),
    ...overrides
  }
}

describe('production upload chooser helpers', () => {
  it('serializes every authenticated delivery scope member', () => {
    const headers = browserUploadDeliveryHeaders('coding', scope, {
      deliveryCredential: 'grant', recipient: 'principal', sourceRecordRevision: 'revision'
    }, 'desktop-token')
    expect(headers.get('X-Hermes-Browser-Transport')).toBe('transport')
    expect(headers.get('X-Hermes-Browser-Sid')).toBe('sid')
    expect(headers.get('X-Hermes-Browser-Task-Generation')).toBe('7')
    expect(headers.get('X-Hermes-Session-Token')).toBe('desktop-token')
    expect([...headers.keys()].filter(key => key.startsWith('x-hermes-browser-'))).toHaveLength(21)
  })

  it('selects exactly one candidate in single mode', async () => {
    const choose = vi.fn(async () => 0 as const)
    const selected = await selectBrowserUploadCandidates(candidates(3), 'selectSingle', choose, new AbortController().signal)
    expect(selected.map(row => row.displayName)).toEqual(['file-0.txt'])
    expect(choose).toHaveBeenCalledTimes(1)
  })

  it('preserves the full 20-file boundary and cancellation', async () => {
    const choose = vi.fn(async () => 0 as const)
    expect(await selectBrowserUploadCandidates(candidates(21), 'selectMultiple', choose, new AbortController().signal)).toHaveLength(20)
    const controller = new AbortController()
    expect(await selectBrowserUploadCandidates(candidates(2), 'selectMultiple', async () => {
      controller.abort(); return 0
    }, controller.signal)).toEqual([])
  })

  it('distinguishes finishing a multi-selection from closing or canceling the picker', async () => {
    const rows = candidates(2)
    let responses = [0, 2] as (0 | 1 | 2 | 3)[]
    await expect(selectBrowserUploadCandidates(
      rows, 'selectMultiple', async () => responses.shift()!, new AbortController().signal
    )).resolves.toEqual([rows[0]])

    responses = [0, 3]
    await expect(selectBrowserUploadCandidates(
      rows, 'selectMultiple', async () => responses.shift()!, new AbortController().signal
    )).resolves.toEqual([])
  })
})

describe('production upload importer', () => {
  it('runs authenticated discovery, grants, staging, assignment, completion cleanup, and revoke', async () => {
    const rows = candidates(2)
    const requests: string[] = []
    const retire = vi.fn(async () => true)
    const assign = vi.fn(async (_id, request) => {
      expect(await request.consume()).toEqual(['/tmp/a', '/tmp/b'])
      await request.settled?.('completed')
      return 'completed' as const
    })
    const result = await importBrowserUpload(chooser(new AbortController().signal), {
      assign, bytes: async function* () { yield new Uint8Array([1]) }, choose: async () => 0,
      consume: async () => ['/tmp/a', '/tmp/b'], retire, scope,
      requestJson: async (requestPath, body) => {
        requests.push(requestPath)
        if (requestPath.endsWith('candidates')) {return { candidates: rows }}
        if (requestPath.endsWith('revoke')) {return { ok: true, revoked: 0 }}
        const index = rows.findIndex(row => row.candidateId === body.candidate_id)
        return {
          deliveryCredential: String(index).padStart(32, 'g'), displayName: rows[index].displayName,
          mimeType: rows[index].mimeType, opaqueRef: String(index).padStart(32, 'o'), recipient: 'principal',
          sha256: String(index).padStart(64, 'a'), size: rows[index].size,
          sourceRecordRevision: rows[index].sourceRecordRevision
        }
      },
      stage: async (_binding, sources) => ({
        expiresAt: Date.now() + 1_000, handle: 'handle',
        files: sources.map(source => ({ displayName: source.displayName, mimeType: source.mimeType, sha256: source.sha256, size: source.size }))
      })
    })
    expect(result).toBe('completed')
    expect(requests).toEqual([
      '/api/browser/upload-sources/candidates', '/api/browser/upload-sources/grant',
      '/api/browser/upload-sources/grant', '/api/browser/upload-sources/revoke'
    ])
    expect(assign).toHaveBeenCalledOnce()
    expect(retire).toHaveBeenCalledWith('handle')
  })

  it('revokes the first grant immediately when a later grant fails', async () => {
    const rows = candidates(2)
    const revoke = vi.fn(async (_body: Record<string, unknown>) => ({ ok: true }))
    let grants = 0
    const requestJson = vi.fn(async (requestPath: string, body: Record<string, unknown>) => {
      if (requestPath.endsWith('candidates')) {return { candidates: rows }}
      if (requestPath.endsWith('revoke')) {return revoke(body)}
      if (++grants === 2) {throw new Error('mint failed')}
      return {
        deliveryCredential: 'g'.repeat(32), displayName: rows[0].displayName, mimeType: rows[0].mimeType,
        opaqueRef: 'o'.repeat(32), recipient: 'principal', sha256: 'a'.repeat(64), size: rows[0].size,
        sourceRecordRevision: rows[0].sourceRecordRevision
      }
    })
    await expect(importBrowserUpload(chooser(new AbortController().signal), {
      assign: vi.fn(), bytes: async function* () {}, choose: async () => 0, consume: vi.fn(), requestJson,
      retire: vi.fn(), scope, stage: vi.fn()
    })).rejects.toThrow('mint failed')
    expect(revoke).toHaveBeenCalledWith(expect.objectContaining({ opaque_refs: ['o'.repeat(32)] }))
  })

  it('revokes a syntactically valid opaque reference from a malformed minted ticket', async () => {
    const rows = candidates(1)
    const requestJson = vi.fn(async (path: string) => {
      if (path.endsWith('candidates')) {return { candidates: rows }}
      if (path.endsWith('revoke')) {return { ok: true }}
      return { ...ticket(rows[0]), mimeType: 'application/unexpected' }
    })

    await expect(importBrowserUpload(chooser(new AbortController().signal), importerDeps(rows, { requestJson })))
      .resolves.toBe('not_started')
    expect(requestJson).toHaveBeenLastCalledWith(
      '/api/browser/upload-sources/revoke',
      expect.objectContaining({ opaque_refs: [ticket(rows[0]).opaqueRef] })
    )
  })

  it('revokes grants when staging fails without attempting assignment', async () => {
    const rows = candidates(1)
    const revoke = vi.fn(async (_body: Record<string, unknown>) => ({ ok: true }))
    const requestJson = vi.fn(async (path: string, body: Record<string, unknown>) => {
      if (path.endsWith('candidates')) {return { candidates: rows }}
      if (path.endsWith('revoke')) {return revoke(body)}
      return ticket(rows[0])
    })
    const assign = vi.fn()

    await expect(importBrowserUpload(chooser(new AbortController().signal), importerDeps(rows, {
      assign, requestJson, stage: async () => {throw new Error('staging failed')}
    }))).rejects.toThrow('staging failed')
    expect(assign).not.toHaveBeenCalled()
    expect(revoke).toHaveBeenCalledOnce()
  })

  it.each(['not_started', 'outcome_unknown'] as const)(
    'handles assignment %s with the correct staging ownership',
    async outcome => {
      const rows = candidates(1)
      const retire = vi.fn(async () => true)
      const requestJson = vi.fn(importerDeps(rows).requestJson)
      await expect(importBrowserUpload(chooser(new AbortController().signal), importerDeps(rows, {
        assign: async () => outcome, requestJson, retire
      }))).resolves.toBe(outcome)
      expect(retire).toHaveBeenCalledTimes(outcome === 'not_started' ? 1 : 0)
      expect(requestJson).toHaveBeenLastCalledWith(
        '/api/browser/upload-sources/revoke', expect.objectContaining({ opaque_refs: [ticket(rows[0]).opaqueRef] })
      )
    }
  )

  it('revokes a grant when cancellation arrives immediately after mint', async () => {
    const rows = candidates(1)
    const controller = new AbortController()
    const requestJson = vi.fn(async (path: string) => {
      if (path.endsWith('candidates')) {return { candidates: rows }}
      if (path.endsWith('revoke')) {return { ok: true }}
      controller.abort()
      return ticket(rows[0])
    })
    const stage = vi.fn()

    await expect(importBrowserUpload(chooser(controller.signal), importerDeps(rows, { requestJson, stage })))
      .resolves.toBe('not_started')
    expect(stage).not.toHaveBeenCalled()
    expect(requestJson).toHaveBeenLastCalledWith(
      '/api/browser/upload-sources/revoke', expect.objectContaining({ opaque_refs: [ticket(rows[0]).opaqueRef] })
    )
  })

  it('preserves the assignment result when best-effort revoke fails', async () => {
    const rows = candidates(1)
    const requestJson = vi.fn(async (path: string) => {
      if (path.endsWith('candidates')) {return { candidates: rows }}
      if (path.endsWith('revoke')) {throw new Error('revoke unavailable')}
      return ticket(rows[0])
    })

    await expect(importBrowserUpload(chooser(new AbortController().signal), importerDeps(rows, { requestJson })))
      .resolves.toBe('completed')
  })
})
