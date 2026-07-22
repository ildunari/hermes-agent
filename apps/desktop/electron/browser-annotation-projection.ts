import type { AnnotationTrustedMainReport } from './browser-annotation-main-report'
import {
  type AnnotationCandidate,
  type AnnotationFrameDescriptor,
  type AnnotationGeometryContext,
  type AnnotationResolution,
  resolveAnnotationAnchor,
  validAnnotationRect
} from './browser-annotation-reporter'
import type { AnnotationScreenshotMarker, AnnotationScreenshotViewport } from './browser-annotation-screenshot'

const MAX_RECORDS = 500
const MAX_RECORD_BYTES = 8 * 1024 * 1024
const MAX_IDENTIFIER = 256
const MAX_REPORT_TAGS = 32

export type AnnotationProjectionHealth = AnnotationResolution['status']

export interface AnnotationSafeProjection {
  annotationId: string
  externalLabel: number
  health: AnnotationProjectionHealth
}

export interface AnnotationProjectionScope {
  documentGeneration: number
  profile: string
  tabId: string
  workspaceId: string
}

export interface AnnotationLabelAuthority {
  labels: Map<string, number>
  nextLabel: number
}

export interface AnnotationTrustedProjection {
  labels: AnnotationLabelAuthority
  markers: readonly AnnotationScreenshotMarker[]
  projections: readonly AnnotationSafeProjection[]
  viewport: AnnotationScreenshotViewport
}

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function identifier(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= MAX_IDENTIFIER
}

function framePath(value: unknown): readonly AnnotationFrameDescriptor[] | null {
  return Array.isArray(value) ? value as readonly AnnotationFrameDescriptor[] : null
}

function sameFramePath(left: readonly AnnotationFrameDescriptor[], right: readonly AnnotationFrameDescriptor[]): boolean {
  return left.length === right.length && left.every((frame, index) => {
    const candidate = right[index]
    return Boolean(candidate) &&
      frame.origin === candidate.origin &&
      frame.committedUrlEvidence === candidate.committedUrlEvidence &&
      (frame.frameName ?? null) === (candidate.frameName ?? null) &&
      (frame.embeddingElementDigest ?? null) === (candidate.embeddingElementDigest ?? null) &&
      Boolean(frame.opaqueOrigin) === Boolean(candidate.opaqueOrigin)
  })
}

function identities(depth: number) {
  return Array.from({ length: depth }, () => [1, 0, 0, 1, 0, 0] as const)
}

function capturedRects(record: Record<string, unknown>) {
  const capture = object(record.capture)
  const rects = capture?.observedTargetRects
  return Array.isArray(rects) && rects.length <= 64 && rects.every(validAnnotationRect) ? rects : undefined
}

function exactGeometry(
  report: AnnotationTrustedMainReport,
  path: readonly AnnotationFrameDescriptor[],
  candidate?: AnnotationCandidate
): AnnotationGeometryContext | null {
  if (candidate) {
    return sameFramePath(path, candidate.framePath) ? report.geometryByFrameId[candidate.frameId] ?? null : null
  }
  const matching = Object.entries(report.framePathByFrameId)
    .filter(([, candidatePath]) => sameFramePath(path, candidatePath))
  return matching.length === 1 ? report.geometryByFrameId[matching[0][0]] ?? null : null
}

function resolveRecord(record: Record<string, unknown>, report: AnnotationTrustedMainReport): AnnotationResolution {
  const anchor = object(record.anchor)
  const path = framePath(anchor?.framePath)
  if (!anchor || !path) {return { reason: 'invalid-anchor', status: 'stale' }}

  if (anchor.type === 'page') {return resolveAnnotationAnchor(anchor, [], {})}

  const captureRects = capturedRects(record)
  if (anchor.type === 'region' || anchor.type === 'drawing') {
    const geometry = exactGeometry(report, path)
    return geometry
      ? resolveAnnotationAnchor(anchor, [], { ...geometry, captureRects })
      : { reason: 'frame-mismatch', status: 'stale' }
  }

  // First resolve semantic identity with neutral geometry. This preserves the
  // resolver's confidence and ambiguity rules without projecting through the
  // wrong frame. Only the unique winner's exact trusted-main geometry is then
  // used for the final projection.
  const identity = resolveAnnotationAnchor(anchor, report.candidates, { frameTransforms: identities(path.length) })
  if (identity.status !== 'resolved' && identity.status !== 'shifted') {return identity}
  if (!('candidate' in identity)) {return { reason: 'invalid-candidate', status: 'stale' }}
  const geometry = exactGeometry(report, path, identity.candidate)
  return geometry
    ? resolveAnnotationAnchor(anchor, report.candidates, { ...geometry, captureRects })
    : { reason: 'invalid-geometry', status: 'stale' }
}

function recordForScope(value: unknown, scope: AnnotationProjectionScope): Record<string, unknown> | null {
  const record = object(value)
  const annotationScope = object(record?.scope)
  if (
    !record || !annotationScope ||
    record.schemaVersion !== 1 ||
    !identifier(record.annotationId) ||
    annotationScope.profileId !== scope.profile ||
    annotationScope.browserWorkspaceId !== scope.workspaceId ||
    annotationScope.tabId !== scope.tabId ||
    annotationScope.documentGenerationId !== String(scope.documentGeneration)
  ) {return null}
  return record
}

function anchorTag(anchor: Record<string, unknown>): string | null {
  const fingerprint = anchor.type === 'element'
    ? object(anchor.fingerprint)
    : anchor.type === 'agent-marker'
      ? object(anchor.promotedElement)
      : anchor.type === 'text'
        ? object(anchor.startElement)
        : anchor.type === 'region'
          ? object(anchor.containingElement)
          : anchor.type === 'drawing'
            ? object(object(anchor.region)?.containingElement)
            : null
  const tag = fingerprint?.tag
  return typeof tag === 'string' && tag.length > 0 && tag.length <= 128 ? tag.toLowerCase() : null
}

/** Validates exact tuple scope before trusted main asks any guest frame for candidates. */
export function annotationProjectionTags(rawRecords: unknown, scope: AnnotationProjectionScope): readonly string[] | null {
  if (!Array.isArray(rawRecords) || rawRecords.length > MAX_RECORDS) {return null}
  const tags = new Set<string>()
  for (const value of rawRecords) {
    const record = recordForScope(value, scope)
    const anchor = object(record?.anchor)
    if (!record || !anchor) {return null}
    const tag = anchorTag(anchor)
    if (tag) {tags.add(tag)}
    if (tags.size > MAX_REPORT_TAGS) {return null}
  }
  return [...tags].sort()
}

/**
 * Resolves backend-authoritative records in trusted main and emits only the
 * side-panel-safe projection. URLs, anchors, page text, geometry, author data,
 * and thread content never cross back through renderer IPC.
 */
export function projectTrustedAnnotations(
  rawRecords: unknown,
  report: AnnotationTrustedMainReport,
  scope: AnnotationProjectionScope,
  labels: AnnotationLabelAuthority
): AnnotationTrustedProjection | null {
  if (
    !Array.isArray(rawRecords) || rawRecords.length > MAX_RECORDS ||
    report.documentGeneration !== scope.documentGeneration ||
    !Number.isSafeInteger(labels.nextLabel) || labels.nextLabel < 1
  ) {return null}
  try {
    if (Buffer.byteLength(JSON.stringify(rawRecords), 'utf8') > MAX_RECORD_BYTES) {return null}
  } catch {return null}

  const nextLabels = new Map(labels.labels)
  let nextLabel = labels.nextLabel
  const projections: AnnotationSafeProjection[] = []
  const markers: AnnotationScreenshotMarker[] = []
  const seen = new Set<string>()

  for (const value of rawRecords) {
    const record = recordForScope(value, scope)
    if (!record || seen.has(record.annotationId as string)) {return null}
    const annotationId = record.annotationId as string
    seen.add(annotationId)
    let externalLabel = nextLabels.get(annotationId)
    if (externalLabel === undefined) {
      if (!Number.isSafeInteger(nextLabel)) {return null}
      externalLabel = nextLabel++
      nextLabels.set(annotationId, externalLabel)
    }
    const resolution = resolveRecord(record, report)
    projections.push({
      annotationId,
      externalLabel,
      health: resolution.status
    })
    if ((resolution.status === 'resolved' || resolution.status === 'shifted') && 'rects' in resolution) {
      markers.push({ externalLabel, rects: resolution.rects })
    }
  }

  let viewport: AnnotationScreenshotViewport | null = null
  for (const geometry of Object.values(report.geometryByFrameId)) {
    const bounds = geometry.surfaceBounds
    if (!bounds) {continue}
    const candidate = { height: bounds.height, width: bounds.width }
    if (!viewport) {viewport = candidate}
    else if (viewport.height !== candidate.height || viewport.width !== candidate.width) {return null}
  }

  return viewport
    ? { labels: { labels: nextLabels, nextLabel }, markers, projections, viewport }
    : null
}

export const ANNOTATION_PROJECTION_LIMITS = Object.freeze({
  maxRecordBytes: MAX_RECORD_BYTES,
  maxRecords: MAX_RECORDS,
  maxReportTags: MAX_REPORT_TAGS
})