import type { BrowserPendingUploadChooser, BrowserUploadAssignmentOutcome } from './browser-guest-security'
import type {
  BrowserUploadStagedHandle,
  BrowserUploadStageSource,
  BrowserUploadStagingBinding
} from './browser-upload-staging'

export interface BrowserUploadCandidate {
  candidateId: string
  displayName: string
  mimeType: string
  size: number
  sourceRecordRevision: string
}

export interface BrowserUploadTicket {
  deliveryCredential: string
  displayName: string
  mimeType: string
  opaqueRef: string
  recipient: string
  sha256: string
  size: number
  sourceRecordRevision: string
}

export interface BrowserUploadImportDeps {
  assign: (
    chooserId: string,
    request: { consume: () => Promise<readonly string[]>; files: readonly {
      displayName: string; mimeType: string; originalDisplayName: string; sha256: string; size: number
    }[]; settled?: (outcome: 'completed' | 'failed' | 'expired') => Promise<void> | void }
  ) => Promise<BrowserUploadAssignmentOutcome>
  bytes: (ticket: BrowserUploadTicket, scope: Record<string, string>, signal: AbortSignal) => AsyncIterable<Uint8Array>
  choose: (candidate: BrowserUploadCandidate, selectedCount: number) => Promise<0 | 1 | 2 | 3>
  requestJson: (path: string, body: Record<string, unknown>) => Promise<unknown>
  scope: Record<string, string>
  stage: (binding: BrowserUploadStagingBinding, sources: readonly BrowserUploadStageSource[]) => Promise<BrowserUploadStagedHandle>
  consume: (handle: string, binding: BrowserUploadStagingBinding) => Promise<readonly string[]>
  retire: (handle: string) => Promise<boolean>
}

function validCandidate(candidate: any): candidate is BrowserUploadCandidate {
  return typeof candidate?.candidateId === 'string' && /^[A-Za-z0-9_-]{32}$/.test(candidate.candidateId) &&
    typeof candidate?.sourceRecordRevision === 'string' && /^[A-Za-z0-9_-]{32}$/.test(candidate.sourceRecordRevision) &&
    typeof candidate?.displayName === 'string' && candidate.displayName.length > 0 && candidate.displayName.length <= 1024 &&
    typeof candidate?.mimeType === 'string' && candidate.mimeType.length > 0 && candidate.mimeType.length <= 256 &&
    Number.isSafeInteger(candidate?.size) && candidate.size >= 0 && candidate.size <= 64 * 1024 * 1024
}

function validTicket(ticket: any, candidate: BrowserUploadCandidate, recipient?: string): ticket is BrowserUploadTicket {
  return typeof ticket?.opaqueRef === 'string' && /^[A-Za-z0-9_-]{32}$/.test(ticket.opaqueRef) &&
    typeof ticket?.deliveryCredential === 'string' && /^[A-Za-z0-9_-]{32}$/.test(ticket.deliveryCredential) &&
    ticket.sourceRecordRevision === candidate.sourceRecordRevision &&
    typeof ticket?.recipient === 'string' && (!recipient || ticket.recipient === recipient) &&
    ticket.displayName === candidate.displayName && ticket.mimeType === candidate.mimeType &&
    ticket.size === candidate.size && typeof ticket?.sha256 === 'string' && /^[a-f0-9]{64}$/.test(ticket.sha256)
}

export async function selectBrowserUploadCandidates(
  candidates: readonly BrowserUploadCandidate[],
  mode: 'selectMultiple' | 'selectSingle',
  choose: (candidate: BrowserUploadCandidate, selectedCount: number) => Promise<0 | 1 | 2 | 3>,
  signal: AbortSignal
): Promise<readonly BrowserUploadCandidate[]> {
  const selected: BrowserUploadCandidate[] = []
  for (const candidate of candidates) {
    if (signal.aborted) {return []}
    if (selected.length >= 20) {break}
    const response = await choose(candidate, selected.length)
    if (signal.aborted) {return []}
    if (response === 0) {
      selected.push(candidate)
      if (mode === 'selectSingle') {break}
    } else if (response === 2) {
      if (mode === 'selectMultiple' && selected.length) {break}
      return []
    } else if (response === 3) {
      return []
    }
  }
  return selected
}

/** Complete production importer. Every minted source grant is revoked in finally. */
export async function importBrowserUpload(
  chooser: Readonly<BrowserPendingUploadChooser>,
  deps: BrowserUploadImportDeps
): Promise<BrowserUploadAssignmentOutcome | 'not_selected'> {
  if (chooser.signal.aborted) {return 'not_selected'}
  const listed = await deps.requestJson('/api/browser/upload-sources/candidates', {
    profile: chooser.profile, scope: deps.scope
  }) as any
  const candidates = Array.isArray(listed?.candidates) ? listed.candidates.filter(validCandidate) : []
  if (!candidates.length || chooser.signal.aborted) {return 'not_selected'}
  const selected = await selectBrowserUploadCandidates(candidates, chooser.mode, deps.choose, chooser.signal)
  if (!selected.length || chooser.signal.aborted) {return 'not_selected'}

  const accepts = chooser.accept.split(',').map(value => value.trim().toLowerCase()).filter(Boolean)
  if (accepts.length && selected.some(candidate => {
    const name = candidate.displayName.toLowerCase()
    const mime = candidate.mimeType.toLowerCase()
    return !accepts.some(value => value.startsWith('.') ? name.endsWith(value) :
      value.endsWith('/*') ? mime.startsWith(value.slice(0, -1)) : mime === value)
  })) {return 'not_selected'}

  const tickets: BrowserUploadTicket[] = []
  const mintedOpaqueRefs: string[] = []
  let staged: BrowserUploadStagedHandle | null = null
  try {
    for (const candidate of selected) {
      if (chooser.signal.aborted) {return 'not_started'}
      const raw = await deps.requestJson('/api/browser/upload-sources/grant', {
        candidate_id: candidate.candidateId,
        source_record_revision: candidate.sourceRecordRevision,
        profile: chooser.profile,
        scope: deps.scope,
        ttl_seconds: 120
      })
      // The opaque reference is independently sufficient to revoke a grant. Capture
      // it before validating the rest of the response so a malformed minted ticket
      // cannot survive until its TTL merely because another field was invalid.
      if (typeof (raw as any)?.opaqueRef === 'string' && /^[A-Za-z0-9_-]{32}$/.test((raw as any).opaqueRef)) {
        mintedOpaqueRefs.push((raw as any).opaqueRef)
      }
      if (!validTicket(raw, candidate, tickets[0]?.recipient)) {return 'not_started'}
      tickets.push(raw)
    }
    if (chooser.signal.aborted) {return 'not_started'}

    const binding: BrowserUploadStagingBinding = {
      authenticatedPrincipal: tickets[0].recipient,
      backendNodeId: deps.scope.backend_node_id,
      bindingGeneration: deps.scope.binding_generation,
      browserSid: deps.scope.browser_sid,
      browserTransportId: deps.scope.transport_id,
      capabilityGeneration: deps.scope.capability_generation,
      chooserId: deps.scope.chooser_id,
      chooserMode: chooser.mode,
      connectionId: deps.scope.connection_id,
      documentGeneration: deps.scope.document_generation,
      formFingerprint: deps.scope.form_fingerprint,
      frameId: deps.scope.frame_id,
      guestGeneration: chooser.guestGeneration,
      origin: deps.scope.origin,
      profile: chooser.profile,
      sourceRecordRevision: tickets.map(ticket => ticket.sourceRecordRevision).join('.'),
      tabId: chooser.tabId,
      tabIncarnation: deps.scope.tab_incarnation,
      taskGeneration: deps.scope.task_generation,
      taskId: chooser.taskId
    }
    const sources = tickets.map(ticket => ({
      bytes: deps.bytes(ticket, deps.scope, chooser.signal),
      displayName: ticket.displayName,
      mimeType: ticket.mimeType,
      sha256: ticket.sha256,
      size: ticket.size
    }))
    staged = await deps.stage(binding, sources)
    if (chooser.signal.aborted) {return 'not_started'}
    const assignedHandle = staged.handle
    const outcome = await deps.assign(chooser.chooserId, {
      consume: () => deps.consume(assignedHandle, binding),
      files: staged.files.map((file, index) => ({
        ...file, originalDisplayName: tickets[index].displayName
      })),
      settled: async () => {await deps.retire(assignedHandle)}
    })
    if (outcome === 'completed' || outcome === 'outcome_unknown') {staged = null}
    return outcome
  } finally {
    if (staged) {await deps.retire(staged.handle).catch(() => false)}
    if (mintedOpaqueRefs.length) {
      await deps.requestJson('/api/browser/upload-sources/revoke', {
        opaque_refs: mintedOpaqueRefs, profile: chooser.profile, scope: deps.scope
      }).catch(() => undefined)
    }
  }
}

export function browserUploadDeliveryHeaders(
  profile: string,
  scope: Record<string, string>,
  ticket: { deliveryCredential: string; recipient: string; sourceRecordRevision: string },
  sessionToken?: string
): Headers {
  const headers = new Headers({
    'X-Hermes-Browser-Grant': ticket.deliveryCredential,
    'X-Hermes-Browser-Source-Record-Revision': ticket.sourceRecordRevision,
    'X-Hermes-Browser-Profile': profile,
    'X-Hermes-Browser-Recipient': ticket.recipient,
    'X-Hermes-Browser-Connection': scope.connection_id,
    'X-Hermes-Browser-Transport': scope.transport_id,
    'X-Hermes-Browser-Sid': scope.browser_sid,
    'X-Hermes-Browser-Capability-Generation': scope.capability_generation,
    'X-Hermes-Browser-Task': scope.task_id,
    'X-Hermes-Browser-Task-Generation': scope.task_generation,
    'X-Hermes-Browser-Tab': scope.tab_id,
    'X-Hermes-Browser-Tab-Incarnation': scope.tab_incarnation,
    'X-Hermes-Browser-Binding-Generation': scope.binding_generation,
    'X-Hermes-Browser-Document-Generation': scope.document_generation,
    'X-Hermes-Browser-Frame': scope.frame_id,
    'X-Hermes-Browser-Origin': scope.origin,
    'X-Hermes-Browser-Chooser': scope.chooser_id,
    'X-Hermes-Browser-Backend-Node': scope.backend_node_id,
    'X-Hermes-Browser-Form-Fingerprint': scope.form_fingerprint,
    'X-Hermes-Browser-Chooser-Mode': scope.chooser_mode,
    'X-Hermes-Browser-Source-Session': scope.source_session_id
  })
  if (sessionToken) {headers.set('X-Hermes-Session-Token', sessionToken)}
  return headers
}
