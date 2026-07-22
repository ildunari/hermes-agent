import crypto from 'node:crypto'

const MAX_CANDIDATES = 256
const MAX_FRAME_DEPTH = 16
const MAX_RECT_COORDINATE = 10_000_000
const MAX_FINGERPRINT_BYTES = 16 * 1024
const MIN_IDENTITY_SCORE = 8
const MIN_RUNNER_UP_MARGIN = 2
const SHIFT_TOLERANCE_CSS_PX = 2

export interface AnnotationRect {
  height: number
  width: number
  x: number
  y: number
}

export interface AnnotationFrameDescriptor {
  committedUrlEvidence: string
  embeddingElementDigest?: null | string
  frameIdHint?: null | string
  frameName?: null | string
  opaqueOrigin?: boolean
  origin: string
}

export interface AnnotationElementFingerprint {
  accessibleNameDigest?: null | string
  ancestorDigests?: readonly string[]
  namespace?: null | string
  role?: null | string
  selectorHints?: readonly string[]
  shadowHostDigests?: readonly string[]
  siblingOrdinal?: null | number
  stableAttributes?: Readonly<Record<string, string>>
  tag: string
  textDigest?: null | string
}

export interface AnnotationCandidate {
  closedShadowRoot?: boolean
  fingerprint: AnnotationElementFingerprint
  frameId: string
  framePath: readonly AnnotationFrameDescriptor[]
  rects: readonly AnnotationRect[]
  visible: boolean
}

export interface AnnotationElementAnchor {
  fingerprint: AnnotationElementFingerprint
  framePath: readonly AnnotationFrameDescriptor[]
  type: 'element'
}

export interface AnnotationAgentMarkerAnchor {
  framePath: readonly AnnotationFrameDescriptor[]
  number: number
  promotedElement: AnnotationElementFingerprint
  ref: string
  snapshotId: string
  type: 'agent-marker'
}

export interface AnnotationTextAnchor {
  direction: 'backward' | 'forward'
  endElement: AnnotationElementFingerprint
  endUtf16Offset: number
  framePath: readonly AnnotationFrameDescriptor[]
  quote: { exact: string; prefix?: string; suffix?: string }
  startElement: AnnotationElementFingerprint
  startUtf16Offset: number
  textNormalizationVersion: 1
  type: 'text'
}

export interface AnnotationPageAnchor {
  framePath: readonly AnnotationFrameDescriptor[]
  type: 'page'
}

export interface AnnotationRegionAnchor {
  documentCssRect: AnnotationRect
  framePath: readonly AnnotationFrameDescriptor[]
  type: 'region'
}

export interface AnnotationDrawingAnchor {
  framePath: readonly AnnotationFrameDescriptor[]
  region: AnnotationRegionAnchor
  type: 'drawing'
}

export type AnnotationResolvableAnchor =
  | AnnotationAgentMarkerAnchor
  | AnnotationDrawingAnchor
  | AnnotationElementAnchor
  | AnnotationPageAnchor
  | AnnotationRegionAnchor
  | AnnotationTextAnchor

/** CSS two-dimensional affine transform `[a,b,c,d,e,f]`. */
export type AnnotationTransform = readonly [number, number, number, number, number, number]

export interface AnnotationGeometryContext {
  captureRects?: readonly AnnotationRect[]
  frameTransforms?: readonly AnnotationTransform[]
  surfaceBounds?: AnnotationRect
}

export type AnnotationResolution =
  | { reason: 'closed-shadow-root'; status: 'unsupported' }
  | {
      reason:
        | 'below-confidence'
        | 'frame-mismatch'
        | 'invalid-anchor'
        | 'invalid-candidate'
        | 'invalid-geometry'
        | 'no-candidate'
      status: 'stale'
    }
  | { candidates: readonly AnnotationCandidate[]; status: 'ambiguous' }
  | { candidate: AnnotationCandidate; rects: readonly AnnotationRect[]; status: 'resolved' | 'shifted' }
  | { rects: readonly AnnotationRect[]; status: 'resolved' | 'shifted' }

function boundedString(value: unknown, max = 512, allowEmpty = false): value is string {
  return typeof value === 'string' && (allowEmpty || value.length > 0) && value.length <= max
}

function validDigest(value: unknown): value is string {
  return typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
}

function hasOnlyKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const allowed = new Set(keys)

  return Object.keys(value).every(key => allowed.has(key))
}

export function digestAnnotationText(value: string): string {
  return crypto.createHash('sha256').update(value.normalize('NFKC').replace(/\s+/g, ' ').trim()).digest('hex')
}

export function validAnnotationRect(rect: unknown): rect is AnnotationRect {
  if (!rect || typeof rect !== 'object' || Array.isArray(rect)) {
    return false
  }
  const candidate = rect as Record<string, unknown>
  const values = [candidate.x, candidate.y, candidate.width, candidate.height]

  return (
    hasOnlyKeys(candidate, ['height', 'width', 'x', 'y']) &&
    values.every(value => typeof value === 'number' && Number.isFinite(value)) &&
    (candidate.width as number) >= 0 &&
    (candidate.height as number) >= 0 &&
    values.every(value => Math.abs(value as number) <= MAX_RECT_COORDINATE)
  )
}

function validStringArray(value: unknown, maxItems: number, digest = false): value is string[] {
  return (
    Array.isArray(value) &&
    value.length <= maxItems &&
    value.every(item => (digest ? validDigest(item) : boundedString(item, 512)))
  )
}

function validFingerprint(value: unknown): value is AnnotationElementFingerprint {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return false
  }
  const candidate = value as Record<string, unknown>

  if (
    !hasOnlyKeys(candidate, [
      'accessibleNameDigest',
      'ancestorDigests',
      'namespace',
      'role',
      'selectorHints',
      'shadowHostDigests',
      'siblingOrdinal',
      'stableAttributes',
      'tag',
      'textDigest'
    ]) ||
    !boundedString(candidate.tag, 128) ||
    (candidate.namespace != null && !boundedString(candidate.namespace, 256)) ||
    (candidate.role != null && !boundedString(candidate.role, 256)) ||
    (candidate.textDigest != null && !validDigest(candidate.textDigest)) ||
    (candidate.accessibleNameDigest != null && !validDigest(candidate.accessibleNameDigest)) ||
    (candidate.siblingOrdinal != null &&
      (!Number.isSafeInteger(candidate.siblingOrdinal) || (candidate.siblingOrdinal as number) < 0)) ||
    (candidate.ancestorDigests != null && !validStringArray(candidate.ancestorDigests, 16, true)) ||
    (candidate.shadowHostDigests != null && !validStringArray(candidate.shadowHostDigests, 16, true)) ||
    (candidate.selectorHints != null && !validStringArray(candidate.selectorHints, 16))
  ) {
    return false
  }

  const attributes = candidate.stableAttributes
  if (attributes != null) {
    if (!attributes || typeof attributes !== 'object' || Array.isArray(attributes)) {
      return false
    }
    const entries = Object.entries(attributes)

    if (entries.length > 32 || entries.some(([key, item]) => !boundedString(key, 128) || !boundedString(item, 512))) {
      return false
    }
  }

  try {
    return Buffer.byteLength(JSON.stringify(candidate), 'utf8') <= MAX_FINGERPRINT_BYTES
  } catch {
    return false
  }
}

function validFrameDescriptor(value: unknown): value is AnnotationFrameDescriptor {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return false
  }
  const frame = value as Record<string, unknown>

  return (
    hasOnlyKeys(frame, [
      'committedUrlEvidence',
      'embeddingElementDigest',
      'frameIdHint',
      'frameName',
      'opaqueOrigin',
      'origin'
    ]) &&
    boundedString(frame.origin, 2048) &&
    boundedString(frame.committedUrlEvidence, 4096) &&
    (frame.frameName == null || boundedString(frame.frameName, 512, true)) &&
    (frame.embeddingElementDigest == null || validDigest(frame.embeddingElementDigest)) &&
    (frame.frameIdHint == null || boundedString(frame.frameIdHint, 256, true)) &&
    (frame.opaqueOrigin == null || typeof frame.opaqueOrigin === 'boolean')
  )
}

function validFramePath(value: unknown): value is AnnotationFrameDescriptor[] {
  return Array.isArray(value) && value.length <= MAX_FRAME_DEPTH && value.every(validFrameDescriptor)
}

function exactFramePath(
  left: readonly AnnotationFrameDescriptor[],
  right: readonly AnnotationFrameDescriptor[]
): boolean {
  if (left.length !== right.length) {
    return false
  }

  return left.every((frame, index) => {
    const candidate = right[index]

    return (
      frame.origin === candidate.origin &&
      frame.committedUrlEvidence === candidate.committedUrlEvidence &&
      (frame.frameName ?? null) === (candidate.frameName ?? null) &&
      (frame.embeddingElementDigest ?? null) === (candidate.embeddingElementDigest ?? null) &&
      Boolean(frame.opaqueOrigin) === Boolean(candidate.opaqueOrigin)
    )
  })
}

export function validAnnotationCandidate(value: unknown): value is AnnotationCandidate {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return false
  }
  const candidate = value as Record<string, unknown>

  return (
    hasOnlyKeys(candidate, ['closedShadowRoot', 'fingerprint', 'frameId', 'framePath', 'rects', 'visible']) &&
    validFingerprint(candidate.fingerprint) &&
    boundedString(candidate.frameId, 256) &&
    validFramePath(candidate.framePath) &&
    typeof candidate.visible === 'boolean' &&
    (candidate.closedShadowRoot == null || typeof candidate.closedShadowRoot === 'boolean') &&
    Array.isArray(candidate.rects) &&
    candidate.rects.length > 0 &&
    candidate.rects.length <= 64 &&
    candidate.rects.every(validAnnotationRect)
  )
}

function overlapScore(expected: readonly string[] = [], actual: readonly string[] = []): number {
  if (expected.length === 0) {
    return 0
  }
  const actualSet = new Set(actual)

  return expected.reduce((score, value) => score + (actualSet.has(value) ? 1 : 0), 0) / expected.length
}

export function scoreAnnotationCandidate(
  expected: AnnotationElementFingerprint,
  candidate: AnnotationElementFingerprint
): number {
  if (expected.tag.toLowerCase() !== candidate.tag.toLowerCase()) {
    return Number.NEGATIVE_INFINITY
  }
  if (expected.namespace && expected.namespace !== candidate.namespace) {
    return Number.NEGATIVE_INFINITY
  }

  let score = 4

  if (expected.role) {
    score += expected.role === candidate.role ? 2 : -2
  }
  if (expected.accessibleNameDigest) {
    score += expected.accessibleNameDigest === candidate.accessibleNameDigest ? 5 : -5
  }
  if (expected.textDigest) {
    score += expected.textDigest === candidate.textDigest ? 4 : -4
  }
  if (expected.siblingOrdinal != null) {
    score += expected.siblingOrdinal === candidate.siblingOrdinal ? 1 : -1
  }

  const expectedAttributes = Object.entries(expected.stableAttributes ?? {})
  if (expectedAttributes.length > 0) {
    const actual = candidate.stableAttributes ?? {}
    const matches = expectedAttributes.filter(([key, value]) => actual[key] === value).length

    score +=
      (matches / expectedAttributes.length) * 4 -
      ((expectedAttributes.length - matches) / expectedAttributes.length) * 4
  }

  score += overlapScore(expected.ancestorDigests, candidate.ancestorDigests) * 2
  score += overlapScore(expected.shadowHostDigests, candidate.shadowHostDigests) * 2
  return score
}

function validTransform(value: unknown): value is AnnotationTransform {
  return (
    Array.isArray(value) &&
    value.length === 6 &&
    value.every(item => typeof item === 'number' && Number.isFinite(item) && Math.abs(item) <= MAX_RECT_COORDINATE)
  )
}

function transformRect(rect: AnnotationRect, transforms: readonly AnnotationTransform[]): AnnotationRect | null {
  let points = [
    { x: rect.x, y: rect.y },
    { x: rect.x + rect.width, y: rect.y },
    { x: rect.x, y: rect.y + rect.height },
    { x: rect.x + rect.width, y: rect.y + rect.height }
  ]

  for (const [a, b, c, d, e, f] of transforms) {
    points = points.map(point => ({ x: a * point.x + c * point.y + e, y: b * point.x + d * point.y + f }))
  }

  const xs = points.map(point => point.x)
  const ys = points.map(point => point.y)
  const result = {
    height: Math.max(...ys) - Math.min(...ys),
    width: Math.max(...xs) - Math.min(...xs),
    x: Math.min(...xs),
    y: Math.min(...ys)
  }

  return validAnnotationRect(result) ? result : null
}

function containedBy(rect: AnnotationRect, surface: AnnotationRect): boolean {
  return (
    rect.x >= surface.x &&
    rect.y >= surface.y &&
    rect.x + rect.width <= surface.x + surface.width &&
    rect.y + rect.height <= surface.y + surface.height
  )
}

export function projectAnnotationRects(
  rects: readonly AnnotationRect[],
  frameDepth: number,
  geometry: AnnotationGeometryContext = {}
): AnnotationRect[] | null {
  if (!Array.isArray(rects) || rects.length === 0 || rects.length > 64 || !rects.every(validAnnotationRect)) {
    return null
  }
  const transforms = geometry.frameTransforms ?? []

  if (!Array.isArray(transforms) || transforms.length !== frameDepth || !transforms.every(validTransform)) {
    return null
  }
  if (geometry.surfaceBounds != null && !validAnnotationRect(geometry.surfaceBounds)) {
    return null
  }
  const projected = rects.map(rect => transformRect(rect, transforms))

  if (projected.some(rect => rect == null)) {
    return null
  }
  const bounded = projected as AnnotationRect[]

  return geometry.surfaceBounds && !bounded.every(rect => containedBy(rect, geometry.surfaceBounds as AnnotationRect))
    ? null
    : bounded
}

function anchorFingerprint(anchor: AnnotationResolvableAnchor): AnnotationElementFingerprint | null {
  if (anchor.type === 'element') {
    return anchor.fingerprint
  }
  if (anchor.type === 'agent-marker') {
    return anchor.promotedElement
  }
  if (anchor.type === 'text') {
    return anchor.startElement
  }

  return null
}

function anchorRegion(anchor: AnnotationResolvableAnchor): AnnotationRect | null {
  if (anchor.type === 'region') {
    return anchor.documentCssRect
  }
  if (anchor.type === 'drawing') {
    return anchor.region?.documentCssRect ?? null
  }

  return null
}

function rectsShifted(current: readonly AnnotationRect[], captured: readonly AnnotationRect[] | undefined): boolean {
  if (!captured || !captured.every(validAnnotationRect)) {
    return false
  }

  if (current.length !== captured.length) {
    return true
  }

  return current.some((rect, index) => {
    const prior = captured[index]

    return (['x', 'y', 'width', 'height'] as const).some(
      key => Math.abs(rect[key] - prior[key]) > SHIFT_TOLERANCE_CSS_PX
    )
  })
}

export function resolveAnnotationAnchor(
  rawAnchor: unknown,
  rawCandidates: unknown,
  geometry: AnnotationGeometryContext = {}
): AnnotationResolution {
  if (!rawAnchor || typeof rawAnchor !== 'object' || Array.isArray(rawAnchor)) {
    return { reason: 'invalid-anchor', status: 'stale' }
  }
  const anchor = rawAnchor as AnnotationResolvableAnchor

  if (!validFramePath(anchor.framePath)) {
    return { reason: 'invalid-anchor', status: 'stale' }
  }
  if (anchor.type === 'page') {
    return { rects: [], status: 'resolved' }
  }
  const region = anchorRegion(anchor)

  if (region) {
    const rects = projectAnnotationRects([region], anchor.framePath.length, geometry)

    return rects
      ? { rects, status: rectsShifted(rects, geometry.captureRects) ? 'shifted' : 'resolved' }
      : { reason: 'invalid-geometry', status: 'stale' }
  }

  const expected = anchorFingerprint(anchor)
  if (
    !expected ||
    !validFingerprint(expected) ||
    !Array.isArray(rawCandidates) ||
    rawCandidates.length > MAX_CANDIDATES
  ) {
    return { reason: 'invalid-anchor', status: 'stale' }
  }
  if (!rawCandidates.every(validAnnotationCandidate)) {
    return { reason: 'invalid-candidate', status: 'stale' }
  }
  const candidates = rawCandidates as AnnotationCandidate[]
  const inFrame = candidates.filter(candidate => exactFramePath(anchor.framePath, candidate.framePath))

  if (inFrame.length === 0 && candidates.length > 0) {
    return { reason: 'frame-mismatch', status: 'stale' }
  }
  const scored = inFrame
    .filter(candidate => candidate.visible)
    .map(candidate => ({ candidate, score: scoreAnnotationCandidate(expected, candidate.fingerprint) }))
    .filter(row => Number.isFinite(row.score))
    .sort((left, right) => right.score - left.score || left.candidate.frameId.localeCompare(right.candidate.frameId))

  if (scored.length === 0) {
    return { reason: 'no-candidate', status: 'stale' }
  }
  if (scored[0].score < MIN_IDENTITY_SCORE) {
    return { reason: 'below-confidence', status: 'stale' }
  }
  const contenders = scored.filter(row => scored[0].score - row.score < MIN_RUNNER_UP_MARGIN)

  if (contenders.some(row => row.candidate.closedShadowRoot)) {
    return contenders.length === 1
      ? { reason: 'closed-shadow-root', status: 'unsupported' }
      : { candidates: contenders.map(row => row.candidate), status: 'ambiguous' }
  }
  if (contenders.length > 1) {
    return { candidates: contenders.map(row => row.candidate), status: 'ambiguous' }
  }
  const rects = projectAnnotationRects(scored[0].candidate.rects, anchor.framePath.length, geometry)

  if (!rects) {
    return { reason: 'invalid-geometry', status: 'stale' }
  }
  return {
    candidate: scored[0].candidate,
    rects,
    status: rectsShifted(rects, geometry.captureRects) ? 'shifted' : 'resolved'
  }
}

export const ANNOTATION_REPORT_LIMITS = Object.freeze({
  maxCandidates: MAX_CANDIDATES,
  maxFingerprintBytes: MAX_FINGERPRINT_BYTES,
  maxFrameDepth: MAX_FRAME_DEPTH,
  maxRectCoordinate: MAX_RECT_COORDINATE,
  minIdentityScore: MIN_IDENTITY_SCORE,
  minRunnerUpMargin: MIN_RUNNER_UP_MARGIN
})
