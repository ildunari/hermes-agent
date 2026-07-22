import { describe, expect, it, vi } from 'vitest'

import {
  ANNOTATION_REPORTER_CALL_SOURCE,
  ANNOTATION_REPORTER_FACTORY_SOURCE,
  collectAnnotationIsolatedWorldReport
} from './browser-annotation-isolated-world'

function debuggerFor(value: unknown) {
  const sendCommand = vi.fn(async (method: string, _params?: Record<string, unknown>) => {
    if (method === 'Page.createIsolatedWorld') {return { executionContextId: 17 }}

    if (method === 'Runtime.evaluate') {return { result: { objectId: 'reporter-1' } }}

    if (method === 'Runtime.callFunctionOn') {return { result: { value } }}

    return {}
  })

  return { debuggerClient: { sendCommand } as never, sendCommand }
}

const viewport = { devicePixelRatio: 2, height: 600, width: 800 }

const semanticReport = {
  candidates: [{
    accessibleName: 'Checkout',
    ancestorTags: ['main'],
    attributes: { 'data-testid': 'checkout' },
    rects: [{ height: 24, width: 100, x: 12, y: 40 }],
    role: 'button',
    shadowHostTags: [],
    siblingOrdinal: 1,
    tag: 'button',
    text: 'Buy now',
    visible: true
  }],
  kind: 'semantic-candidates',
  viewport
}

describe('fixed isolated-world annotation reporter', () => {
  it('creates a fresh fixed world, passes request data by value, and releases the reporter object', async () => {
    const { debuggerClient, sendCommand } = debuggerFor(semanticReport)
    const request = { kind: 'semantic-candidates' as const, tags: ['button', 'a'] }

    await expect(collectAnnotationIsolatedWorldReport(debuggerClient, 'frame-1', request)).resolves.toEqual(semanticReport)
    expect(sendCommand).toHaveBeenNthCalledWith(1, 'Page.createIsolatedWorld', {
      frameId: 'frame-1',
      grantUniveralAccess: false,
      worldName: 'hermes-browser-reporter-1004'
    })
    expect(sendCommand).toHaveBeenNthCalledWith(2, 'Runtime.evaluate', expect.objectContaining({
      expression: ANNOTATION_REPORTER_FACTORY_SOURCE,
      returnByValue: false
    }))
    expect(sendCommand).toHaveBeenNthCalledWith(3, 'Runtime.callFunctionOn', {
      arguments: [{ value: request }],
      awaitPromise: false,
      functionDeclaration: ANNOTATION_REPORTER_CALL_SOURCE,
      objectId: 'reporter-1',
      returnByValue: true
    })
    expect(sendCommand).toHaveBeenNthCalledWith(4, 'Runtime.releaseObject', { objectId: 'reporter-1' })
    expect(ANNOTATION_REPORTER_FACTORY_SOURCE).not.toContain('button,a')
  })

  it('preserves the fixed executable bytes across hostile request strings', async () => {
    const first = debuggerFor({ ...semanticReport, candidates: [] })
    const second = debuggerFor({ ...semanticReport, candidates: [] })
    const hostile = 'x"]});globalThis.pwned=true;//'

    await expect(
      collectAnnotationIsolatedWorldReport(first.debuggerClient, 'frame-1', { kind: 'semantic-candidates', tags: ['button'] })
    ).resolves.toBeTruthy()
    await expect(
      collectAnnotationIsolatedWorldReport(second.debuggerClient, 'frame-1', { kind: 'semantic-candidates', tags: [hostile] })
    ).rejects.toThrow('browser-annotation-report-invalid')

    const firstEvaluate = first.sendCommand.mock.calls.find(([method]) => method === 'Runtime.evaluate')
    expect(firstEvaluate?.[1]).toEqual(expect.objectContaining({ expression: ANNOTATION_REPORTER_FACTORY_SOURCE }))
    expect(second.sendCommand).not.toHaveBeenCalled()
    expect(ANNOTATION_REPORTER_FACTORY_SOURCE).not.toContain(hostile)
  })

  it('accepts the bounded viewport response used by the production guest report path', async () => {
    const { debuggerClient } = debuggerFor(viewport)
    await expect(collectAnnotationIsolatedWorldReport(debuggerClient, 'frame-1', { kind: 'viewport' })).resolves.toEqual(viewport)
  })

  it('fails closed on malformed and oversized hostile results while still releasing the object', async () => {
    const malformed = debuggerFor({ ...semanticReport, candidates: [{ ...semanticReport.candidates[0], extra: true }] })
    await expect(
      collectAnnotationIsolatedWorldReport(malformed.debuggerClient, 'frame-1', { kind: 'semantic-candidates', tags: ['button'] })
    ).rejects.toThrow('browser-annotation-report-invalid')
    expect(malformed.sendCommand).toHaveBeenLastCalledWith('Runtime.releaseObject', { objectId: 'reporter-1' })

    const oversized = debuggerFor({ ...semanticReport, candidates: [{ ...semanticReport.candidates[0], text: 'x'.repeat(300_000) }] })
    await expect(
      collectAnnotationIsolatedWorldReport(oversized.debuggerClient, 'frame-1', { kind: 'semantic-candidates', tags: ['button'] })
    ).rejects.toThrow('browser-annotation-report-invalid')
    expect(oversized.sendCommand).toHaveBeenLastCalledWith('Runtime.releaseObject', { objectId: 'reporter-1' })
  })

  it('rejects invalid frame and request bounds before attaching the debugger world', async () => {
    const { debuggerClient, sendCommand } = debuggerFor(viewport)
    await expect(collectAnnotationIsolatedWorldReport(debuggerClient, '', { kind: 'viewport' })).rejects.toThrow(
      'browser-annotation-report-invalid'
    )
    await expect(
      collectAnnotationIsolatedWorldReport(debuggerClient, 'frame-1', {
        kind: 'semantic-candidates',
        tags: Array.from({ length: 33 }, () => 'button')
      })
    ).rejects.toThrow('browser-annotation-report-invalid')
    expect(sendCommand).not.toHaveBeenCalled()
  })
})
