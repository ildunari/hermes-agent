import { describe, expect, it } from 'vitest'

import {
  type AnnotationCandidate,
  type AnnotationElementFingerprint,
  type AnnotationFrameDescriptor,
  digestAnnotationText,
  projectAnnotationRects,
  resolveAnnotationAnchor,
  scoreAnnotationCandidate,
  validAnnotationCandidate,
  validAnnotationRect
} from './browser-annotation-reporter'

const digest = (value: string) => digestAnnotationText(value)
const frame: AnnotationFrameDescriptor = {
  committedUrlEvidence: 'https://example.test/page',
  opaqueOrigin: false,
  origin: 'https://example.test'
}
const identity = [1, 0, 0, 1, 0, 0] as const

function fingerprint(overrides: Partial<AnnotationElementFingerprint> = {}): AnnotationElementFingerprint {
  return {
    accessibleNameDigest: digest('Checkout'),
    ancestorDigests: [digest('main')],
    role: 'button',
    siblingOrdinal: 1,
    stableAttributes: { 'data-testid': 'checkout' },
    tag: 'button',
    textDigest: digest('Buy now'),
    ...overrides
  }
}

function candidate(overrides: Partial<AnnotationCandidate> = {}): AnnotationCandidate {
  return {
    fingerprint: fingerprint(),
    frameId: 'top',
    framePath: [frame],
    rects: [{ height: 24, width: 100, x: 12, y: 40 }],
    visible: true,
    ...overrides
  }
}

const geometry = {
  frameTransforms: [identity],
  surfaceBounds: { height: 800, width: 1200, x: 0, y: 0 }
}

describe('browser annotation semantic resolver', () => {
  it('normalizes semantic text before hashing', () => {
    expect(digest('  Buy\n now ')).toBe(digest('Buy now'))
    expect(digest('Ｂｕｙ')).toBe(digest('Buy'))
  })

  it('chooses one confident semantic candidate and derives shifted only from geometry', () => {
    const exact = candidate()
    const anchor = { fingerprint: fingerprint(), framePath: [frame], type: 'element' }

    expect(resolveAnnotationAnchor(anchor, [exact], geometry)).toMatchObject({ candidate: exact, status: 'resolved' })
    expect(
      resolveAnnotationAnchor(anchor, [exact], {
        ...geometry,
        captureRects: [{ height: 24, width: 100, x: 1, y: 1 }]
      })
    ).toMatchObject({ candidate: exact, status: 'shifted' })
  })

  it('refuses weak identity and requires a meaningful runner-up margin', () => {
    const anchor = { fingerprint: fingerprint(), framePath: [frame], type: 'element' }
    const unrelated = candidate({
      fingerprint: fingerprint({
        accessibleNameDigest: digest('Other'),
        role: 'link',
        siblingOrdinal: 9,
        stableAttributes: { id: 'other' },
        textDigest: digest('Other')
      })
    })

    expect(resolveAnnotationAnchor(anchor, [unrelated], geometry)).toEqual({
      reason: 'below-confidence',
      status: 'stale'
    })
    expect(
      resolveAnnotationAnchor(anchor, [candidate({ frameId: 'a' }), candidate({ frameId: 'b' })], geometry)
    ).toMatchObject({
      status: 'ambiguous'
    })
  })

  it('binds candidates to the exact semantic frame path', () => {
    const foreignFrame = { ...frame, origin: 'https://foreign.test' }
    const anchor = { fingerprint: fingerprint(), framePath: [frame], type: 'element' }

    expect(resolveAnnotationAnchor(anchor, [candidate({ framePath: [foreignFrame] })], geometry)).toEqual({
      reason: 'frame-mismatch',
      status: 'stale'
    })
    expect(resolveAnnotationAnchor({ ...anchor, framePath: [] }, [candidate()], { frameTransforms: [] })).toEqual({
      reason: 'frame-mismatch',
      status: 'stale'
    })
  })

  it('handles closed-shadow contenders deterministically', () => {
    const anchor = { fingerprint: fingerprint(), framePath: [frame], type: 'element' }
    const open = candidate({ frameId: 'open' })
    const closed = candidate({ closedShadowRoot: true, frameId: 'closed' })

    expect(resolveAnnotationAnchor(anchor, [closed], geometry)).toEqual({
      reason: 'closed-shadow-root',
      status: 'unsupported'
    })
    expect(resolveAnnotationAnchor(anchor, [closed, open], geometry)).toMatchObject({ status: 'ambiguous' })
    expect(resolveAnnotationAnchor(anchor, [open, closed], geometry)).toMatchObject({ status: 'ambiguous' })
  })

  it('projects affine nested-frame transforms and rejects unbound geometry', () => {
    expect(
      projectAnnotationRects([{ height: 10, width: 20, x: 3, y: 4 }], 2, {
        frameTransforms: [
          [1, 0, 0, 1, 10, 20],
          [2, 0, 0, 2, 2, 3]
        ],
        surfaceBounds: { height: 500, width: 500, x: 0, y: 0 }
      })
    ).toEqual([{ height: 20, width: 40, x: 28, y: 51 }])
    expect(projectAnnotationRects([{ height: 10, width: 20, x: 3, y: 4 }], 1, { frameTransforms: [] })).toBeNull()
    expect(validAnnotationRect({ height: 1, width: 1, x: Number.NaN, y: 0 })).toBe(false)
    expect(validAnnotationRect({ height: -1, width: 1, x: 0, y: 0 })).toBe(false)
  })

  it('strictly rejects malformed and oversized hostile candidate reports', () => {
    const malformed = {
      ...candidate(),
      closedShadowRoot: 'yes',
      extra: true,
      fingerprint: { ...fingerprint(), role: 42, selectorHints: Array.from({ length: 100_000 }, () => 'x') }
    }

    expect(validAnnotationCandidate(malformed)).toBe(false)
    expect(
      resolveAnnotationAnchor(
        { fingerprint: fingerprint(), framePath: [frame], type: 'element' },
        [malformed],
        geometry
      )
    ).toEqual({
      reason: 'invalid-candidate',
      status: 'stale'
    })
    expect(
      resolveAnnotationAnchor({ fingerprint: fingerprint(), framePath: [frame], type: 'element' }, null, geometry)
    ).toEqual({
      reason: 'invalid-anchor',
      status: 'stale'
    })
  })

  it('accepts authoritative P4.1 drawing and agent-marker wire shapes', () => {
    expect(
      resolveAnnotationAnchor(
        {
          framePath: [frame],
          region: {
            documentCssRect: { height: 50, width: 100, x: 20, y: 30 },
            framePath: [frame],
            normalizedCropRect: { height: 0.1, width: 0.1, x: 0.1, y: 0.1 },
            type: 'region'
          },
          strokes: [],
          type: 'drawing'
        },
        [],
        geometry
      )
    ).toEqual({ rects: [{ height: 50, width: 100, x: 20, y: 30 }], status: 'resolved' })

    expect(
      resolveAnnotationAnchor(
        {
          framePath: [frame],
          number: 7,
          promotedElement: fingerprint(),
          ref: '@e7',
          snapshotId: 'snapshot-1',
          type: 'agent-marker'
        },
        [candidate()],
        geometry
      )
    ).toMatchObject({ status: 'resolved' })
  })

  it('fails closed for malformed drawings and reports changed rect counts as shifted', () => {
    expect(resolveAnnotationAnchor({ framePath: [frame], type: 'drawing' }, [], geometry)).toEqual({
      reason: 'invalid-anchor',
      status: 'stale'
    })
    expect(
      resolveAnnotationAnchor(
        { fingerprint: fingerprint(), framePath: [frame], type: 'element' },
        [candidate()],
        {
          ...geometry,
          captureRects: [
            { height: 24, width: 100, x: 12, y: 40 },
            { height: 10, width: 10, x: 120, y: 40 }
          ]
        }
      )
    ).toMatchObject({ status: 'shifted' })
  })

  it('accepts P4.1 text anchors through their start element without weakening confidence', () => {
    expect(
      resolveAnnotationAnchor(
        {
          direction: 'forward',
          endElement: fingerprint(),
          endUtf16Offset: 7,
          framePath: [frame],
          quote: { exact: 'Buy now' },
          startElement: fingerprint(),
          startUtf16Offset: 0,
          textNormalizationVersion: 1,
          type: 'text'
        },
        [candidate()],
        geometry
      )
    ).toMatchObject({ status: 'resolved' })
  })

  it('weights semantic identity above selector-style hints', () => {
    const expected = fingerprint({ selectorHints: ['#checkout'] })
    const semantic = fingerprint({ selectorHints: ['.changed'] })
    const selectorOnly = fingerprint({
      accessibleNameDigest: digest('Wrong'),
      selectorHints: ['#checkout'],
      textDigest: digest('Wrong')
    })

    expect(scoreAnnotationCandidate(expected, semantic)).toBeGreaterThan(
      scoreAnnotationCandidate(expected, selectorOnly)
    )
  })
})
