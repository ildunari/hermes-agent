import type { WebContents } from 'electron'

const REPORTER_WORLD_NAME = 'hermes-browser-reporter-1004'
const MAX_REPORT_BYTES = 256 * 1024
const MAX_TAGS = 32
const MAX_CANDIDATES = 256
const MAX_RECTS = 64
const MAX_STRING = 512

export type AnnotationIsolatedWorldRequest =
  | { kind: 'semantic-candidates'; tags: readonly string[] }
  | { kind: 'viewport' }

export interface AnnotationRawCandidate {
  accessibleName: string
  ancestorTags: readonly string[]
  attributes: Readonly<Record<string, string>>
  rects: readonly { height: number; width: number; x: number; y: number }[]
  role: string
  shadowHostTags: readonly string[]
  siblingOrdinal: number
  tag: string
  text: string
  visible: boolean
}

export interface AnnotationRawSemanticReport {
  candidates: readonly AnnotationRawCandidate[]
  kind: 'semantic-candidates'
  viewport: AnnotationViewportReport
}

export interface AnnotationViewportReport {
  devicePixelRatio: number
  height: number
  width: number
}

export type AnnotationIsolatedWorldReport = AnnotationRawSemanticReport | AnnotationViewportReport

type DebuggerClient = Pick<WebContents['debugger'], 'sendCommand'>

// This source is a build-time constant. Request data is passed only as a CDP
// value to Runtime.callFunctionOn, never interpolated into executable source.
export const ANNOTATION_REPORTER_FACTORY_SOURCE = `(() => {
  const viewport = () => ({
    devicePixelRatio: Number(globalThis.devicePixelRatio) || 1,
    height: Math.max(0, Math.floor(globalThis.innerHeight || 0)),
    width: Math.max(0, Math.floor(globalThis.innerWidth || 0))
  });
  const text = value => String(value || '').normalize('NFKC').replace(/\\s+/g, ' ').trim().slice(0, 512);
  const attributes = element => {
    const result = {};
    for (const attribute of Array.from(element.attributes || [])) {
      const name = String(attribute.name || '').toLowerCase();
      if (name === 'id' || name === 'name' || name === 'type' || name === 'role' || name === 'data-testid' || name.startsWith('aria-')) {
        result[name.slice(0, 128)] = text(attribute.value);
      }
      if (Object.keys(result).length >= 32) break;
    }
    return result;
  };
  const rects = element => Array.from(element.getClientRects()).slice(0, 64).map(rect => ({
    height: Number(rect.height), width: Number(rect.width), x: Number(rect.x), y: Number(rect.y)
  }));
  const visit = (root, tags, ancestors, shadowHosts, output) => {
    for (const element of Array.from(root.querySelectorAll(':scope > *'))) {
      if (output.length >= 256) return;
      const tag = String(element.localName || '').toLowerCase().slice(0, 128);
      if (tags.has(tag)) {
        const ownRects = rects(element);
        output.push({
          accessibleName: text(element.getAttribute('aria-label') || element.getAttribute('title') || ''),
          ancestorTags: ancestors.slice(-16), attributes: attributes(element), rects: ownRects,
          role: text(element.getAttribute('role') || ''), shadowHostTags: shadowHosts.slice(-16),
          siblingOrdinal: Math.max(0, Array.prototype.indexOf.call(element.parentElement?.children || [], element)),
          tag, text: text(element.textContent),
          visible: ownRects.some(rect => rect.width > 0 && rect.height > 0) && globalThis.getComputedStyle(element).visibility !== 'hidden'
        });
      }
      visit(element, tags, ancestors.concat(tag).slice(-16), shadowHosts, output);
      if (element.shadowRoot) visit(element.shadowRoot, tags, ancestors, shadowHosts.concat(tag).slice(-16), output);
    }
  };
  return {
    collect(request) {
      if (!request || request.kind === 'viewport') return viewport();
      const tags = new Set(Array.isArray(request.tags) ? request.tags.slice(0, 32).map(tag => String(tag).toLowerCase()) : []);
      const candidates = [];
      visit(document, tags, [], [], candidates);
      return { candidates, kind: 'semantic-candidates', viewport: viewport() };
    }
  };
})()`

export const ANNOTATION_REPORTER_CALL_SOURCE = 'function(request) { return this.collect(request); }'

function validNumber(value: unknown, min: number, max: number): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max
}

function validViewport(value: unknown): value is AnnotationViewportReport {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {return false}
  const viewport = value as Record<string, unknown>

  return (
    Object.keys(viewport).every(key => ['devicePixelRatio', 'height', 'width'].includes(key)) &&
    validNumber(viewport.devicePixelRatio, 0.1, 100) &&
    validNumber(viewport.height, 0, 10_000_000) &&
    validNumber(viewport.width, 0, 10_000_000)
  )
}

function validString(value: unknown, max = MAX_STRING): value is string {
  return typeof value === 'string' && value.length <= max
}

function validCandidate(value: unknown): value is AnnotationRawCandidate {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {return false}
  const candidate = value as Record<string, unknown>

  const allowed = [
    'accessibleName', 'ancestorTags', 'attributes', 'rects', 'role', 'shadowHostTags',
    'siblingOrdinal', 'tag', 'text', 'visible'
  ]

  if (!Object.keys(candidate).every(key => allowed.includes(key))) {return false}

  if (!validString(candidate.accessibleName) || !validString(candidate.role) || !validString(candidate.tag, 128) ||
      !validString(candidate.text) || typeof candidate.visible !== 'boolean' ||
      !Number.isSafeInteger(candidate.siblingOrdinal) || (candidate.siblingOrdinal as number) < 0) {return false}

  if (!Array.isArray(candidate.ancestorTags) || candidate.ancestorTags.length > 16 ||
      !candidate.ancestorTags.every(item => validString(item, 128))) {return false}

  if (!Array.isArray(candidate.shadowHostTags) || candidate.shadowHostTags.length > 16 ||
      !candidate.shadowHostTags.every(item => validString(item, 128))) {return false}

  if (!candidate.attributes || typeof candidate.attributes !== 'object' || Array.isArray(candidate.attributes)) {return false}
  const attributes = Object.entries(candidate.attributes as Record<string, unknown>)

  if (attributes.length > 32 || attributes.some(([key, item]) => !validString(key, 128) || !validString(item))) {return false}

  if (!Array.isArray(candidate.rects) || candidate.rects.length > MAX_RECTS) {return false}

  return candidate.rects.every(rect => {
    if (!rect || typeof rect !== 'object' || Array.isArray(rect)) {return false}
    const value = rect as Record<string, unknown>

    return Object.keys(value).every(key => ['height', 'width', 'x', 'y'].includes(key)) &&
      validNumber(value.height, 0, 10_000_000) && validNumber(value.width, 0, 10_000_000) &&
      validNumber(value.x, -10_000_000, 10_000_000) && validNumber(value.y, -10_000_000, 10_000_000)
  })
}

function validateReport(request: AnnotationIsolatedWorldRequest, value: unknown): AnnotationIsolatedWorldReport | null {
  let encoded: string

  try {
    encoded = JSON.stringify(value)
  } catch {
    return null
  }

  if (typeof encoded !== 'string' || Buffer.byteLength(encoded, 'utf8') > MAX_REPORT_BYTES) {return null}

  if (request.kind === 'viewport') {return validViewport(value) ? value : null}

  if (!value || typeof value !== 'object' || Array.isArray(value)) {return null}
  const report = value as Record<string, unknown>

  if (!Object.keys(report).every(key => ['candidates', 'kind', 'viewport'].includes(key)) ||
      report.kind !== 'semantic-candidates' || !validViewport(report.viewport) ||
      !Array.isArray(report.candidates) || report.candidates.length > MAX_CANDIDATES ||
      !report.candidates.every(validCandidate)) {return null}

  return value as AnnotationRawSemanticReport
}

function validateRequest(request: AnnotationIsolatedWorldRequest): boolean {
  return request.kind === 'viewport' || (
    request.kind === 'semantic-candidates' && Array.isArray(request.tags) && request.tags.length > 0 &&
    request.tags.length <= MAX_TAGS && request.tags.every(tag => /^[a-z][a-z0-9-]{0,127}$/i.test(tag))
  )
}

export async function collectAnnotationIsolatedWorldReport(
  debuggerClient: DebuggerClient,
  frameId: string,
  request: AnnotationIsolatedWorldRequest,
  sessionId?: string
): Promise<AnnotationIsolatedWorldReport> {
  if (!frameId || frameId.length > 256 || !validateRequest(request)) {throw new Error('browser-annotation-report-invalid')}
  const send = (method: string, params: Record<string, unknown>) => sessionId
    ? debuggerClient.sendCommand(method, params, sessionId)
    : debuggerClient.sendCommand(method, params)

  const isolated = await send('Page.createIsolatedWorld', {
    frameId,
    grantUniveralAccess: false,
    worldName: REPORTER_WORLD_NAME
  })

  if (!Number.isSafeInteger(isolated?.executionContextId)) {throw new Error('browser-annotation-world-invalid')}

  const factory = await send('Runtime.evaluate', {
    awaitPromise: false,
    contextId: isolated.executionContextId,
    expression: ANNOTATION_REPORTER_FACTORY_SOURCE,
    returnByValue: false
  })

  const objectId = factory?.result?.objectId

  if (typeof objectId !== 'string' || objectId.length === 0 || objectId.length > 512) {
    throw new Error('browser-annotation-factory-invalid')
  }

  try {
    const evaluated = await send('Runtime.callFunctionOn', {
      arguments: [{ value: request }],
      awaitPromise: false,
      functionDeclaration: ANNOTATION_REPORTER_CALL_SOURCE,
      objectId,
      returnByValue: true
    })

    const report = validateReport(request, evaluated?.result?.value)

    if (!report) {throw new Error('browser-annotation-report-invalid')}

    return report
  } finally {
    await send('Runtime.releaseObject', { objectId }).catch(() => undefined)
  }
}

export const ANNOTATION_ISOLATED_WORLD_LIMITS = Object.freeze({
  maxCandidates: MAX_CANDIDATES,
  maxReportBytes: MAX_REPORT_BYTES,
  maxTags: MAX_TAGS
})
