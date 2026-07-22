import type { WebContents } from 'electron'

import {
  type AnnotationIsolatedWorldRequest,
  type AnnotationRawCandidate,
  type AnnotationRawSemanticReport,
  collectAnnotationIsolatedWorldReport
} from './browser-annotation-isolated-world'
import {
  type AnnotationCandidate,
  type AnnotationFrameDescriptor,
  type AnnotationGeometryContext,
  type AnnotationRect,
  type AnnotationTransform,
  digestAnnotationText
} from './browser-annotation-reporter'

const MAX_FRAMES = 128
const MAX_FRAME_DEPTH = 16
const MAX_FRAME_STRING = 4096

interface CdpFrame {
  id: string
  name: string
  parentId: null | string
  securityOrigin: string
  url: string
}

interface CollectedFrame {
  descriptor: AnnotationFrameDescriptor
  frame: CdpFrame
  ownerSessionId?: string
  report: AnnotationRawSemanticReport
}

export interface AnnotationTrustedMainReport {
  candidates: readonly AnnotationCandidate[]
  documentGeneration: number
  framePathByFrameId: Readonly<Record<string, readonly AnnotationFrameDescriptor[]>>
  geometryByFrameId: Readonly<Record<string, AnnotationGeometryContext>>
}

type DebuggerClient = Pick<WebContents['debugger'], 'sendCommand'>

interface AnnotationMainReportRequest {
  debuggerClient: DebuggerClient
  documentGeneration: number
  frameSessions: ReadonlyMap<string, string>
  isCurrent: () => boolean
  tags: readonly string[]
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function boundedString(value: unknown, max = MAX_FRAME_STRING, allowEmpty = false): value is string {
  return typeof value === 'string' && value.length <= max && (allowEmpty || value.length > 0)
}

function parseFrame(value: unknown): CdpFrame | null {
  const raw = record(value)
  if (!raw || !boundedString(raw.id, 256) || !boundedString(raw.url, MAX_FRAME_STRING, true)) {return null}

  return {
    id: raw.id,
    name: boundedString(raw.name, 512, true) ? raw.name : '',
    parentId: boundedString(raw.parentId, 256) ? raw.parentId : null,
    securityOrigin: boundedString(raw.securityOrigin, 2048, true) ? raw.securityOrigin : '',
    url: raw.url
  }
}

function mergeFrame(left: CdpFrame, right: CdpFrame): CdpFrame | null {
  const mergeField = (first: string, second: string) => {
    if (first && second && first !== second) {return null}
    return first || second
  }
  const name = mergeField(left.name, right.name)
  const securityOrigin = mergeField(left.securityOrigin, right.securityOrigin)
  const url = mergeField(left.url, right.url)
  const parentsConflict = left.parentId !== null && right.parentId !== null && left.parentId !== right.parentId

  return name === null || securityOrigin === null || url === null || parentsConflict
    ? null
    : { id: left.id, name, parentId: left.parentId ?? right.parentId, securityOrigin, url }
}

function flattenFrameTree(value: unknown, output: CdpFrame[]): boolean {
  const tree = record(value)
  const frame = parseFrame(tree?.frame)
  if (!tree || !frame || output.length >= MAX_FRAMES) {return false}
  output.push(frame)

  if (tree.childFrames === undefined) {return true}
  if (!Array.isArray(tree.childFrames) || output.length + tree.childFrames.length > MAX_FRAMES) {return false}
  return tree.childFrames.every(child => flattenFrameTree(child, output))
}

function frameDescriptor(frame: CdpFrame, embeddingElementDigest: null | string): AnnotationFrameDescriptor {
  let origin = frame.securityOrigin
  let opaqueOrigin = !origin || origin === 'null'

  if (!origin || origin === 'null') {
    try {
      const parsed = new URL(frame.url)
      origin = parsed.origin
      opaqueOrigin = origin === 'null'
    } catch {
      origin = 'opaque'
      opaqueOrigin = true
    }
  }

  if (opaqueOrigin) {origin = 'opaque'}

  return {
    committedUrlEvidence: frame.url,
    embeddingElementDigest,
    frameIdHint: frame.id,
    frameName: frame.name,
    opaqueOrigin,
    origin
  }
}

function digestFrameOwner(value: unknown): null | string {
  const node = record(record(value)?.node)
  if (!node) {return null}
  const nodeName = boundedString(node.nodeName, 128, true) ? node.nodeName.toLowerCase() : ''
  const rawAttributes = node.attributes
  if (!Array.isArray(rawAttributes) || rawAttributes.length > 64 || rawAttributes.some(item => !boundedString(item, 512, true))) {
    return nodeName ? digestAnnotationText(nodeName) : null
  }

  const allowed = new Set(['id', 'name', 'role', 'title', 'data-testid'])
  const attributes: string[] = []
  for (let index = 0; index + 1 < rawAttributes.length; index += 2) {
    const key = rawAttributes[index].toLowerCase()
    if (allowed.has(key) || key.startsWith('aria-')) {
      attributes.push(`${key}=${rawAttributes[index + 1]}`)
    }
  }
  return digestAnnotationText([nodeName, ...attributes.sort()].join('\0'))
}

function transformForOwner(value: unknown, viewport: { height: number; width: number }): AnnotationTransform | null {
  const model = record(record(value)?.model)
  const content = model?.content
  if (
    !Array.isArray(content) || content.length !== 8 ||
    content.some(item => typeof item !== 'number' || !Number.isFinite(item)) ||
    viewport.width <= 0 || viewport.height <= 0
  ) {return null}

  const [x1, y1, x2, y2, , , x4, y4] = content as number[]
  const transform: AnnotationTransform = [
    (x2 - x1) / viewport.width,
    (y2 - y1) / viewport.width,
    (x4 - x1) / viewport.height,
    (y4 - y1) / viewport.height,
    x1,
    y1
  ]
  return transform.every(item => Number.isFinite(item) && Math.abs(item) <= 10_000_000) ? transform : null
}

function candidateFingerprint(raw: AnnotationRawCandidate) {
  const attributes = Object.fromEntries(
    Object.entries(raw.attributes)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, value]) => [key, digestAnnotationText(value)])
  )

  return {
    accessibleNameDigest: raw.accessibleName ? digestAnnotationText(raw.accessibleName) : null,
    ancestorDigests: raw.ancestorTags.map(digestAnnotationText),
    role: raw.role || null,
    shadowHostDigests: raw.shadowHostTags.map(digestAnnotationText),
    siblingOrdinal: raw.siblingOrdinal,
    stableAttributes: attributes,
    tag: raw.tag,
    textDigest: raw.text ? digestAnnotationText(raw.text) : null
  }
}

async function send(
  client: DebuggerClient,
  method: string,
  params: Record<string, unknown>,
  sessionId?: string
): Promise<unknown> {
  return sessionId
    ? client.sendCommand(method, params, sessionId)
    : client.sendCommand(method, params)
}

function pathFor(frameId: string, frames: ReadonlyMap<string, CollectedFrame>): CollectedFrame[] | null {
  const reversed: CollectedFrame[] = []
  const seen = new Set<string>()
  let current = frames.get(frameId)

  while (current) {
    if (seen.has(current.frame.id) || reversed.length >= MAX_FRAME_DEPTH) {return null}
    seen.add(current.frame.id)
    reversed.push(current)
    current = current.frame.parentId ? frames.get(current.frame.parentId) : undefined
  }

  if (reversed.length === 0 || reversed[reversed.length - 1].frame.parentId !== null) {return null}
  return reversed.reverse()
}

/**
 * Collects raw semantics and frame geometry entirely in Electron main. The
 * returned object is resolver input, not renderer IPC: page text and attribute
 * values have already been reduced to SHA-256 digests.
 */
export async function collectAnnotationTrustedMainReport(
  request: AnnotationMainReportRequest
): Promise<AnnotationTrustedMainReport> {
  if (!Number.isSafeInteger(request.documentGeneration) || request.documentGeneration <= 0 || !request.isCurrent()) {
    throw new Error('browser-annotation-main-stale')
  }

  const sessionIds = [...new Set(request.frameSessions.values())]
  if (sessionIds.length >= MAX_FRAMES) {throw new Error('browser-annotation-main-invalid')}
  const snapshots = await Promise.all([
    send(request.debuggerClient, 'Page.getFrameTree', {}),
    ...sessionIds.map(sessionId => send(request.debuggerClient, 'Page.getFrameTree', {}, sessionId))
  ])
  if (!request.isCurrent()) {throw new Error('browser-annotation-main-stale')}

  const rawFrames: CdpFrame[] = []
  for (const snapshot of snapshots) {
    const tree = record(snapshot)?.frameTree
    if (!flattenFrameTree(tree, rawFrames)) {throw new Error('browser-annotation-main-invalid')}
  }

  const uniqueFrames = new Map<string, CdpFrame>()
  for (const frame of rawFrames) {
    const previous = uniqueFrames.get(frame.id)
    const merged = previous ? mergeFrame(previous, frame) : frame
    if (!merged) {throw new Error('browser-annotation-main-invalid')}
    uniqueFrames.set(frame.id, merged)
  }
  if (uniqueFrames.size === 0 || uniqueFrames.size > MAX_FRAMES) {throw new Error('browser-annotation-main-invalid')}

  const ownerSessionByFrame = new Map<string, string | undefined>()
  const sessionRootIds = new Set(request.frameSessions.keys())
  const resolveOwnerSession = (frame: CdpFrame): string | undefined => {
    if (ownerSessionByFrame.has(frame.id)) {return ownerSessionByFrame.get(frame.id)}
    let current: CdpFrame | undefined = frame
    const seen = new Set<string>()
    while (current) {
      if (seen.has(current.id)) {throw new Error('browser-annotation-main-invalid')}
      seen.add(current.id)
      if (sessionRootIds.has(current.id)) {
        const session = request.frameSessions.get(current.id)
        ownerSessionByFrame.set(frame.id, session)
        return session
      }
      current = current.parentId ? uniqueFrames.get(current.parentId) : undefined
    }
    ownerSessionByFrame.set(frame.id, undefined)
    return undefined
  }

  const reports = new Map<string, AnnotationRawSemanticReport>()
  for (const frame of uniqueFrames.values()) {
    if (!request.isCurrent()) {throw new Error('browser-annotation-main-stale')}
    const value = await collectAnnotationIsolatedWorldReport(
      request.debuggerClient,
      frame.id,
      { kind: 'semantic-candidates', tags: request.tags } satisfies AnnotationIsolatedWorldRequest,
      resolveOwnerSession(frame)
    )
    if (!('kind' in value) || value.kind !== 'semantic-candidates') {throw new Error('browser-annotation-main-invalid')}
    reports.set(frame.id, value as AnnotationRawSemanticReport)
  }

  const ownerDigests = new Map<string, null | string>()
  const ownerTransforms = new Map<string, AnnotationTransform>()
  const unavailableFrames = new Set<string>()
  for (const frame of uniqueFrames.values()) {
    if (!frame.parentId) {continue}
    const parent = uniqueFrames.get(frame.parentId)
    const report = reports.get(frame.id)
    if (!parent || !report) {throw new Error('browser-annotation-main-invalid')}
    if (unavailableFrames.has(parent.id)) {
      unavailableFrames.add(frame.id)
      continue
    }
    const parentSession = resolveOwnerSession(parent)
    try {
      const owner = record(await send(request.debuggerClient, 'Page.getFrameOwner', { frameId: frame.id }, parentSession))
      const backendNodeId = owner?.backendNodeId
      if (!Number.isSafeInteger(backendNodeId) || (backendNodeId as number) <= 0) {throw new Error('frame-owner-invalid')}
      const [described, box] = await Promise.all([
        send(request.debuggerClient, 'DOM.describeNode', { backendNodeId }, parentSession),
        send(request.debuggerClient, 'DOM.getBoxModel', { backendNodeId }, parentSession)
      ])
      const transform = transformForOwner(box, report.viewport)
      if (!transform) {throw new Error('frame-transform-invalid')}
      ownerDigests.set(frame.id, digestFrameOwner(described))
      ownerTransforms.set(frame.id, transform)
    } catch {
      // Hidden, detached, or layout-less frames have no trustworthy projection.
      // Exclude that subtree instead of making unrelated root annotations
      // unavailable or inventing geometry.
      unavailableFrames.add(frame.id)
    }
  }
  if (!request.isCurrent()) {throw new Error('browser-annotation-main-stale')}

  const collected = new Map<string, CollectedFrame>()
  for (const frame of uniqueFrames.values()) {
    const report = reports.get(frame.id)
    if (!report) {throw new Error('browser-annotation-main-invalid')}
    collected.set(frame.id, {
      descriptor: frameDescriptor(frame, ownerDigests.get(frame.id) ?? null),
      frame,
      ownerSessionId: resolveOwnerSession(frame),
      report
    })
  }

  const candidates: AnnotationCandidate[] = []
  const framePathByFrameId: Record<string, readonly AnnotationFrameDescriptor[]> = {}
  const geometryByFrameId: Record<string, AnnotationGeometryContext> = {}
  for (const frame of collected.values()) {
    const path = pathFor(frame.frame.id, collected)
    if (!path) {throw new Error('browser-annotation-main-invalid')}
    if (path.some(item => unavailableFrames.has(item.frame.id))) {continue}
    framePathByFrameId[frame.frame.id] = path.map(item => item.descriptor)
    const transforms: AnnotationTransform[] = []
    for (let index = path.length - 1; index > 0; index -= 1) {
      const transform = ownerTransforms.get(path[index].frame.id)
      if (!transform) {throw new Error('browser-annotation-main-invalid')}
      transforms.push(transform)
    }
    transforms.push([1, 0, 0, 1, 0, 0])
    geometryByFrameId[frame.frame.id] = {
      frameTransforms: transforms,
      surfaceBounds: {
        height: path[0].report.viewport.height,
        width: path[0].report.viewport.width,
        x: 0,
        y: 0
      }
    }
    for (const raw of frame.report.candidates) {
      if (raw.rects.length === 0) {continue}
      candidates.push({
        fingerprint: candidateFingerprint(raw),
        frameId: frame.frame.id,
        framePath: path.map(item => item.descriptor),
        rects: raw.rects as readonly AnnotationRect[],
        visible: raw.visible
      })
    }
  }

  return {
    candidates,
    documentGeneration: request.documentGeneration,
    framePathByFrameId,
    geometryByFrameId
  }
}

export const ANNOTATION_MAIN_REPORT_LIMITS = Object.freeze({
  maxFrameDepth: MAX_FRAME_DEPTH,
  maxFrames: MAX_FRAMES
})
