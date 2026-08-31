import type { ReactNode } from 'react'

import { isDesktopFsRemoteMode, readDesktopFileDataUrl } from '@/lib/desktop-fs'
import type { PreviewTarget } from '@/store/preview'

import { ContribBoundary, ContribRender } from './react/boundary'
import { useContributions } from './react/use-contributions'
import type { Contribution } from './types'

export const ATTACHMENT_PREVIEWERS_AREA = 'attachment.previewers'
export const ATTACHMENT_PREVIEW_MAX_BYTES = 32 * 1024 * 1024

const HOST_TYPES = {
  '.docx': {
    mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    type: 'docx'
  },
  '.pdf': { mimeType: 'application/pdf', type: 'pdf' }
} as const

type HostAttachmentExtension = keyof typeof HOST_TYPES
export type AttachmentPreviewType = (typeof HOST_TYPES)[HostAttachmentExtension]['type']

export interface AttachmentPreviewInfo {
  byteSize?: number
  extension: HostAttachmentExtension
  label: string
  mimeType: string
}

export interface AttachmentPreviewSource {
  byteSize: number
  dataUrl: string
  mimeType: string
}

export interface AttachmentPreviewRenderProps {
  attachment: AttachmentPreviewInfo
  load: () => Promise<AttachmentPreviewSource>
  reloadKey: number
}

export interface AttachmentPreviewerContribution {
  extensions: readonly HostAttachmentExtension[]
  maxBytes?: number
  mimeTypes: readonly string[]
  render: (props: AttachmentPreviewRenderProps) => ReactNode
}

function literalExtension(value: string): string {
  const index = value.lastIndexOf('.')

  return index >= 0 ? value.slice(index).toLowerCase() : ''
}

function targetExtension(target: PreviewTarget): string {
  // Filesystem paths treat `?` and `#` as ordinary filename characters. Only
  // URL fallback candidates have query/fragment syntax to strip.
  const literal = target.path?.trim() || target.source?.trim() || target.label?.trim()

  if (literal) {
    return literalExtension(literal)
  }

  try {
    return literalExtension(new URL(target.url).pathname)
  } catch {
    return ''
  }
}

function mime(value: string | undefined): string {
  return value?.split(';', 1)[0]?.trim().toLowerCase() || ''
}

function pathHasTraversal(value: string): boolean {
  let candidate = value

  try {
    candidate = decodeURIComponent(candidate)
  } catch {
    return true
  }

  return candidate.replace(/\\/g, '/').split('/').includes('..')
}

function targetPath(target: PreviewTarget): string {
  if (target.kind !== 'file') {
    throw new Error('Attachment preview is available only for file attachments')
  }

  const url = target.url?.trim()

  if (url && /^[a-z][a-z0-9+.-]*:/i.test(url) && !url.toLowerCase().startsWith('file:')) {
    throw new Error('Attachment preview rejected a non-file URL')
  }

  const candidate = target.path?.trim() || target.source?.trim()

  if (!candidate || /^https?:\/\//i.test(candidate) || pathHasTraversal(candidate)) {
    throw new Error('Attachment preview rejected an unsafe attachment path')
  }

  return candidate
}

function dataUrlByteSize(payload: string): number {
  if (!payload || payload.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(payload)) {
    throw new Error('Attachment preview returned malformed base64 data')
  }

  const padding = payload.endsWith('==') ? 2 : payload.endsWith('=') ? 1 : 0

  return (payload.length / 4) * 3 - padding
}

function validateSource(
  source: AttachmentPreviewSource,
  expectedMime: string,
  maxBytes: number
): AttachmentPreviewSource {
  const match = /^data:([^;,]+);base64,([A-Za-z0-9+/]*={0,2})$/.exec(source.dataUrl)

  if (!match || mime(match[1]) !== expectedMime || mime(source.mimeType) !== expectedMime) {
    throw new Error('Attachment preview returned an unexpected MIME type')
  }

  const byteSize = dataUrlByteSize(match[2])

  if (byteSize !== source.byteSize || byteSize > maxBytes) {
    throw new Error(`Attachment preview exceeds the ${maxBytes}-byte limit`)
  }

  return source
}

export function selectAttachmentPreviewer(
  target: PreviewTarget,
  contributions: readonly Contribution[]
): { contribution: Contribution; data: AttachmentPreviewerContribution; extension: HostAttachmentExtension } | null {
  if (target.kind !== 'file') {
    return null
  }

  const ext = targetExtension(target) as HostAttachmentExtension
  const host = HOST_TYPES[ext]

  if (!host) {
    return null
  }

  const targetMime = mime(target.mimeType)

  for (const contribution of contributions) {
    const data = contribution.data as AttachmentPreviewerContribution | undefined

    if (!data?.extensions?.includes(ext) || !data.mimeTypes?.map(mime).includes(host.mimeType)) {
      continue
    }

    if (targetMime && targetMime !== host.mimeType && targetMime !== 'application/octet-stream') {
      continue
    }

    return { contribution, data, extension: ext }
  }

  return null
}

export async function loadAttachmentPreviewSource(
  target: PreviewTarget,
  previewer: AttachmentPreviewerContribution,
  extensionValue: HostAttachmentExtension,
  dependencies: { readRemote?: (path: string) => Promise<string>; remote?: boolean } = {}
): Promise<AttachmentPreviewSource> {
  const spec = HOST_TYPES[extensionValue]
  const filePath = targetPath(target)
  const requestedMax = Number(previewer.maxBytes)

  const maxBytes = Math.min(
    ATTACHMENT_PREVIEW_MAX_BYTES,
    Number.isFinite(requestedMax) && requestedMax > 0 ? requestedMax : ATTACHMENT_PREVIEW_MAX_BYTES
  )

  if ((target.byteSize ?? 0) > maxBytes) {
    throw new Error(`Attachment preview exceeds the ${maxBytes}-byte limit`)
  }

  const remote = dependencies.remote ?? isDesktopFsRemoteMode()
  let source: AttachmentPreviewSource

  if (remote) {
    const dataUrl = await (dependencies.readRemote ?? readDesktopFileDataUrl)(filePath)
    const payload = dataUrl.slice(dataUrl.indexOf(',') + 1)
    source = { byteSize: dataUrlByteSize(payload), dataUrl, mimeType: spec.mimeType }
  } else {
    const read = window.hermesDesktop?.readAttachmentPreview

    if (!read) {
      throw new Error('Secure attachment previews require a newer Hermes Desktop shell')
    }

    source = await read({ path: filePath, type: spec.type })
  }

  return validateSource(source, spec.mimeType, maxBytes)
}

export function AttachmentPreviewHost({
  fallback,
  reloadKey,
  target
}: {
  fallback: ReactNode
  reloadKey: number
  target: PreviewTarget
}) {
  const contributions = useContributions(ATTACHMENT_PREVIEWERS_AREA)
  const selected = selectAttachmentPreviewer(target, contributions)

  if (!selected) {
    return fallback
  }

  const attachment: AttachmentPreviewInfo = {
    byteSize: target.byteSize,
    extension: selected.extension,
    label: target.label,
    mimeType: HOST_TYPES[selected.extension].mimeType
  }

  const render = () =>
    selected.data.render({
      attachment,
      load: () => loadAttachmentPreviewSource(target, selected.data, selected.extension),
      reloadKey
    })

  return (
    <ContribBoundary id={selected.contribution.id}>
      <ContribRender render={render} />
    </ContribBoundary>
  )
}
