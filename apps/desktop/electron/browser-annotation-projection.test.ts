import { describe, expect, it } from 'vitest'

import type { AnnotationTrustedMainReport } from './browser-annotation-main-report'
import { type AnnotationLabelAuthority, projectTrustedAnnotations } from './browser-annotation-projection'
import { digestAnnotationText } from './browser-annotation-reporter'

const identity = [1, 0, 0, 1, 0, 0] as const
const frame = {
  committedUrlEvidence: 'https://example.test/page',
  opaqueOrigin: false,
  origin: 'https://example.test'
}
const fingerprint = {
  accessibleNameDigest: digestAnnotationText('Checkout'),
  ancestorDigests: [digestAnnotationText('main')],
  role: 'button',
  siblingOrdinal: 1,
  stableAttributes: { 'data-testid': digestAnnotationText('checkout') },
  tag: 'button',
  textDigest: digestAnnotationText('Buy now')
}

function report(candidates = [candidate()]): AnnotationTrustedMainReport {
  return {
    candidates,
    documentGeneration: 3,
    framePathByFrameId: { top: [frame] },
    geometryByFrameId: {
      top: {
        frameTransforms: [identity],
        surfaceBounds: { height: 800, width: 1200, x: 0, y: 0 }
      }
    }
  }
}

function candidate(overrides: Record<string, unknown> = {}) {
  return {
    fingerprint,
    frameId: 'top',
    framePath: [frame],
    rects: [{ height: 24, width: 100, x: 12, y: 40 }],
    visible: true,
    ...overrides
  }
}

function record(annotationId: string, overrides: Record<string, unknown> = {}) {
  return {
    annotationId,
    anchor: { fingerprint, framePath: [frame], type: 'element' },
    capture: { observedTargetRects: [{ height: 24, width: 100, x: 12, y: 40 }] },
    schemaVersion: 1,
    scope: {
      browserWorkspaceId: 'workspace-1',
      documentGenerationId: '3',
      profileId: 'coding',
      tabId: 'browser:one'
    },
    ...overrides
  }
}

const scope = {
  documentGeneration: 3,
  profile: 'coding',
  tabId: 'browser:one',
  workspaceId: 'workspace-1'
}
const labels = (): AnnotationLabelAuthority => ({ labels: new Map(), nextLabel: 1 })

describe('trusted-main annotation projection', () => {
  it('returns only safe health and monotonic non-recycled external labels', () => {
    const first = projectTrustedAnnotations([record('a'), record('b')], report(), scope, labels())

    expect(first?.projections).toEqual([
      { annotationId: 'a', externalLabel: 1, health: 'resolved' },
      { annotationId: 'b', externalLabel: 2, health: 'resolved' }
    ])

    const removed = projectTrustedAnnotations([record('b')], report(), scope, first!.labels)
    const restored = projectTrustedAnnotations([record('a'), record('c')], report(), scope, removed!.labels)

    expect(restored?.projections).toEqual([
      { annotationId: 'a', externalLabel: 1, health: 'resolved' },
      { annotationId: 'c', externalLabel: 3, health: 'resolved' }
    ])
    expect(Object.keys(restored!.projections[0]).sort()).toEqual(['annotationId', 'externalLabel', 'health'])
  })

  it('preserves ambiguity and closed-shadow unsupported outcomes', () => {
    const ambiguous = report([
      candidate({ frameId: 'a' }),
      candidate({ frameId: 'b' })
    ])
    expect(projectTrustedAnnotations([record('a')], ambiguous, scope, labels())?.projections[0].health).toBe('ambiguous')

    const closed = report([candidate({ closedShadowRoot: true })])
    expect(projectTrustedAnnotations([record('a')], closed, scope, labels())?.projections[0].health).toBe('unsupported')
  })

  it('fails the whole request closed on cross-scope, stale-generation, duplicate, or oversized input', () => {
    expect(projectTrustedAnnotations([
      record('a'),
      record('foreign', { scope: { ...record('x').scope as object, profileId: 'other' } })
    ], report(), scope, labels())).toBeNull()
    expect(projectTrustedAnnotations([record('a'), record('a')], report(), scope, labels())).toBeNull()
    expect(projectTrustedAnnotations([record('a')], { ...report(), documentGeneration: 4 }, scope, labels())).toBeNull()
    expect(projectTrustedAnnotations(Array.from({ length: 501 }, (_, index) => record(String(index))), report(), scope, labels())).toBeNull()
  })

  it('uses exact frame geometry for region anchors and refuses duplicate frame identity', () => {
    const region = record('region', {
      anchor: {
        documentCssRect: { height: 20, width: 30, x: 10, y: 12 },
        framePath: [frame],
        normalizedCropRect: { height: 0.1, width: 0.1, x: 0, y: 0 },
        type: 'region'
      }
    })
    expect(projectTrustedAnnotations([region], report([]), scope, labels())?.projections[0].health).toBe('shifted')

    const duplicateFrame = report([])
    duplicateFrame.framePathByFrameId = { one: [frame], two: [frame] }
    duplicateFrame.geometryByFrameId = {
      one: duplicateFrame.geometryByFrameId.top,
      two: duplicateFrame.geometryByFrameId.top
    }
    expect(projectTrustedAnnotations([region], duplicateFrame, scope, labels())?.projections[0].health).toBe('stale')
  })
})
