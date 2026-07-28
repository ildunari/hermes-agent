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
  const candidate = target.path || target.source || target.url

  const documentKind = DOCUMENT_PREVIEW_KIND_BY_EXT[
    extension(candidate) as keyof typeof DOCUMENT_PREVIEW_KIND_BY_EXT
  ]

  if (!documentKind || target.previewKind === documentKind) {
    return target
  }

  return { ...target, previewKind: documentKind }
}

export function visualDocumentPreviewKind(value: string): 'docx' | 'pdf' | undefined {
  return DOCUMENT_PREVIEW_KIND_BY_EXT[extension(value) as keyof typeof DOCUMENT_PREVIEW_KIND_BY_EXT]
}
