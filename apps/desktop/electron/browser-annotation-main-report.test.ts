import { describe, expect, it, vi } from 'vitest'

import { collectAnnotationTrustedMainReport } from './browser-annotation-main-report'
import { digestAnnotationText, resolveAnnotationAnchor } from './browser-annotation-reporter'

const viewport = { devicePixelRatio: 2, height: 600, width: 800 }
const childViewport = { devicePixelRatio: 2, height: 100, width: 200 }

function semantic(text: string, child = false) {
  return {
    candidates: [{
      accessibleName: text,
      ancestorTags: ['main'],
      attributes: { 'data-testid': text },
      rects: [{ height: 20, width: 40, x: 10, y: 5 }],
      role: 'button',
      shadowHostTags: [],
      siblingOrdinal: 0,
      tag: 'button',
      text,
      visible: true
    }],
    kind: 'semantic-candidates',
    viewport: child ? childViewport : viewport
  }
}

function fixtureDebugger() {
  const frameBySession = new Map<string, string>()
  const sendCommand = vi.fn(async (method: string, params: Record<string, unknown> = {}, sessionId?: string) => {
    const session = sessionId ?? 'root'
    if (method === 'Page.getFrameTree') {
      return sessionId
        ? { frameTree: { frame: {
            id: 'child', name: 'checkout', parentId: 'root', securityOrigin: 'https://child.test', url: 'https://child.test/pay'
          } } }
        : { frameTree: { frame: {
            id: 'root', name: '', securityOrigin: 'https://example.test', url: 'https://example.test/page'
          } } }
    }
    if (method === 'Page.createIsolatedWorld') {
      frameBySession.set(session, params.frameId as string)
      return { executionContextId: sessionId ? 22 : 11 }
    }
    if (method === 'Runtime.evaluate') {return { result: { objectId: `reporter-${session}` } }}
    if (method === 'Runtime.callFunctionOn') {
      return { result: { value: semantic(frameBySession.get(session) === 'child' ? 'Pay now secret' : 'Root secret', sessionId !== undefined) } }
    }
    if (method === 'Page.getFrameOwner') {return { backendNodeId: 7 }}
    if (method === 'DOM.describeNode') {
      return { node: { attributes: ['data-testid', 'payment-frame'], nodeName: 'IFRAME' } }
    }
    if (method === 'DOM.getBoxModel') {
      return { model: { content: [100, 50, 300, 50, 300, 150, 100, 150] } }
    }
    return {}
  })

  return { debuggerClient: { sendCommand } as never, sendCommand }
}

describe('trusted-main annotation report', () => {
  it('collects an OOPIF in its attached session, digests semantics, and builds affine geometry', async () => {
    const { debuggerClient, sendCommand } = fixtureDebugger()
    const result = await collectAnnotationTrustedMainReport({
      debuggerClient,
      documentGeneration: 4,
      frameSessions: new Map([['child', 'session-child']]),
      isCurrent: () => true,
      tags: ['button']
    })

    expect(result.documentGeneration).toBe(4)
    expect(result.candidates).toHaveLength(2)
    expect(JSON.stringify(result)).not.toContain('secret')
    expect(JSON.stringify(result)).not.toContain('payment-frame')
    expect(result.candidates[1].fingerprint).toMatchObject({
      accessibleNameDigest: digestAnnotationText('Pay now secret'),
      stableAttributes: { 'data-testid': digestAnnotationText('Pay now secret') },
      textDigest: digestAnnotationText('Pay now secret')
    })
    expect(result.candidates[1].framePath).toHaveLength(2)
    expect(result.geometryByFrameId.child.frameTransforms).toEqual([
      [1, 0, 0, 1, 100, 50],
      [1, 0, 0, 1, 0, 0]
    ])
    expect(sendCommand).toHaveBeenCalledWith('Page.createIsolatedWorld', expect.objectContaining({ frameId: 'child' }), 'session-child')
    expect(sendCommand).toHaveBeenCalledWith('DOM.getBoxModel', { backendNodeId: 7 })
  })

  it('feeds exact frame-bound candidates and transforms into the resolver', async () => {
    const { debuggerClient } = fixtureDebugger()
    const result = await collectAnnotationTrustedMainReport({
      debuggerClient,
      documentGeneration: 1,
      frameSessions: new Map([['child', 'session-child']]),
      isCurrent: () => true,
      tags: ['button']
    })
    const child = result.candidates.find(candidate => candidate.frameId === 'child')!
    const resolution = resolveAnnotationAnchor(
      { fingerprint: child.fingerprint, framePath: child.framePath, type: 'element' },
      result.candidates,
      result.geometryByFrameId.child
    )

    expect(resolution).toMatchObject({
      rects: [{ height: 20, width: 40, x: 110, y: 55 }],
      status: 'resolved'
    })
  })

  it('keeps root annotations when a hidden frame has no box and merges partial duplicate OOPIF metadata', async () => {
    const { debuggerClient, sendCommand } = fixtureDebugger()
    sendCommand.mockImplementation(async (method: string, params: Record<string, unknown> = {}, sessionId?: string) => {
      const session = sessionId ?? 'root'
      if (method === 'Page.getFrameTree') {
        return sessionId
          ? { frameTree: { frame: {
              id: 'child', name: 'checkout', parentId: 'root', securityOrigin: 'https://child.test', url: 'https://child.test/pay'
            } } }
          : { frameTree: {
              childFrames: [{ frame: {
                id: 'child', name: '', parentId: 'root', securityOrigin: '', url: 'https://child.test/pay'
              } }],
              frame: {
                id: 'root', name: '', securityOrigin: 'https://example.test', url: 'https://example.test/page'
              }
            } }
      }
      if (method === 'Page.createIsolatedWorld') {return { executionContextId: sessionId ? 22 : 11 }}
      if (method === 'Runtime.evaluate') {return { result: { objectId: `reporter-${session}` } }}
      if (method === 'Runtime.callFunctionOn') {return { result: { value: semantic(sessionId ? 'Child secret' : 'Root secret', Boolean(sessionId)) } }}
      if (method === 'Runtime.releaseObject') {return {}}
      if (method === 'Page.getFrameOwner') {return { backendNodeId: 7 }}
      if (method === 'DOM.describeNode') {return { node: { attributes: [], nodeName: 'IFRAME' } }}
      if (method === 'DOM.getBoxModel') {throw new Error('Could not compute box model')}
      return {}
    })

    const result = await collectAnnotationTrustedMainReport({
      debuggerClient,
      documentGeneration: 1,
      frameSessions: new Map([['child', 'session-child']]),
      isCurrent: () => true,
      tags: ['button']
    })

    expect(result.candidates.map(candidate => candidate.frameId)).toEqual(['root'])
    expect(result.geometryByFrameId.root).toBeDefined()
    expect(result.geometryByFrameId.child).toBeUndefined()
  })

  it('fails stale after frame discovery and rejects malformed frame ownership', async () => {
    const stale = fixtureDebugger()
    let current = true
    stale.sendCommand.mockImplementationOnce(async () => {
      current = false
      return { frameTree: { frame: { id: 'root', name: '', securityOrigin: 'https://example.test', url: 'https://example.test' } } }
    })
    await expect(collectAnnotationTrustedMainReport({
      debuggerClient: stale.debuggerClient,
      documentGeneration: 1,
      frameSessions: new Map(),
      isCurrent: () => current,
      tags: ['button']
    })).rejects.toThrow('browser-annotation-main-stale')

    const malformed = fixtureDebugger()
    malformed.sendCommand.mockImplementation(async (method: string, _params?: Record<string, unknown>, sessionId?: string) => {
      if (method === 'Page.getFrameTree') {
        return sessionId
          ? { frameTree: { frame: { id: 'child', name: '', parentId: 'missing', securityOrigin: 'https://child.test', url: 'https://child.test' } } }
          : { frameTree: { frame: { id: 'root', name: '', securityOrigin: 'https://example.test', url: 'https://example.test' } } }
      }
      return {}
    })
    await expect(collectAnnotationTrustedMainReport({
      debuggerClient: malformed.debuggerClient,
      documentGeneration: 1,
      frameSessions: new Map([['child', 'session-child']]),
      isCurrent: () => true,
      tags: ['button']
    })).rejects.toThrow()
  })
})
