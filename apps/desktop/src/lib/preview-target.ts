import type { PreviewTarget } from '@/store/preview'

const DOCUMENT_PREVIEW_KIND_BY_EXT = {
  '.docx': 'docx',
  '.pdf': 'pdf'
} as const

function extension(value: string) {
  const clean = value.split(/[?#]/, 1)[0] || value
  const idx = clean.lastIndexOf('.')

  return idx >= 0 ? clean.slice(idx).toLowerCase() : ''
}

export function isVisualDocumentPath(value: string): boolean {
  return extension(value) in DOCUMENT_PREVIEW_KIND_BY_EXT
}

export function normalizeDocumentPreviewKind(target: PreviewTarget): PreviewTarget {
  if (target.previewKind !== 'binary') {
    return target
  }

  if (target.mimeType?.split(';', 1)[0]?.trim().toLowerCase() === 'application/pdf') {
    return { ...target, previewKind: 'pdf' }
  }

  const candidate = target.path || target.source || target.url
  const candidateExtension = target.path
    ? candidate.slice(candidate.lastIndexOf('.')).toLowerCase()
    : extension(candidate)
  const documentKind = DOCUMENT_PREVIEW_KIND_BY_EXT[
    candidateExtension as keyof typeof DOCUMENT_PREVIEW_KIND_BY_EXT
  ]

  if (!documentKind) {
    return target
  }

  return { ...target, previewKind: documentKind }
}

export function visualDocumentPreviewKind(value: string): 'docx' | 'pdf' | undefined {
  return DOCUMENT_PREVIEW_KIND_BY_EXT[extension(value) as keyof typeof DOCUMENT_PREVIEW_KIND_BY_EXT]
}
