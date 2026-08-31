import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { resolveReadableFileForIpc } from './hardening'

export const ATTACHMENT_PREVIEW_MAX_BYTES = 32 * 1024 * 1024

export const ATTACHMENT_PREVIEW_TYPES = {
  docx: {
    extension: '.docx',
    mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
  },
  pdf: { extension: '.pdf', mimeType: 'application/pdf' }
} as const

export type AttachmentPreviewType = keyof typeof ATTACHMENT_PREVIEW_TYPES

export interface AttachmentPreviewRequest {
  path: string
  type: AttachmentPreviewType
}

export interface AttachmentPreviewResult {
  byteSize: number
  dataUrl: string
  mimeType: string
}

function pathFromRequest(value: unknown): string {
  const raw = String(value || '').trim()

  if (!raw) {
    throw Object.assign(new Error('Attachment preview failed: file path is required.'), { code: 'invalid-path' })
  }

  let candidate = raw

  const windowsDrivePath = /^[a-z]:[\\/]/i.test(raw)

  if (!windowsDrivePath && /^[a-z][a-z0-9+.-]*:/i.test(raw)) {
    let url: URL

    try {
      url = new URL(raw)
    } catch {
      throw Object.assign(new Error('Attachment preview failed: URL is invalid.'), { code: 'invalid-path' })
    }

    if (url.protocol !== 'file:') {
      throw Object.assign(new Error('Attachment preview failed: only local file paths are allowed.'), {
        code: 'invalid-scheme'
      })
    }

    candidate = fileURLToPath(url)
  }

  // Reject traversal syntax before path.resolve can erase it. The host passes
  // normalized attachment targets, so a dot-dot segment is never necessary.
  if (candidate.replace(/\\/g, '/').split('/').includes('..')) {
    throw Object.assign(new Error('Attachment preview failed: traversal is not allowed.'), { code: 'path-traversal' })
  }

  if (!path.isAbsolute(candidate)) {
    throw Object.assign(new Error('Attachment preview failed: an absolute path is required.'), { code: 'invalid-path' })
  }

  return candidate
}

export async function readAttachmentPreviewForIpc(
  request: AttachmentPreviewRequest,
  fsImpl: typeof fs = fs
): Promise<AttachmentPreviewResult> {
  const type = request?.type
  const spec = ATTACHMENT_PREVIEW_TYPES[type]

  if (!spec) {
    throw Object.assign(new Error('Attachment preview failed: unsupported document type.'), {
      code: 'unsupported-type'
    })
  }

  const requestedPath = pathFromRequest(request?.path)

  if (path.extname(requestedPath).toLowerCase() !== spec.extension) {
    throw Object.assign(new Error(`Attachment preview failed: expected a ${spec.extension} file.`), {
      code: 'extension-mismatch'
    })
  }

  const lstat = await fsImpl.promises.lstat(requestedPath).catch(error => {
    const code = error && typeof error === 'object' && 'code' in error ? String(error.code) : 'read-error'
    throw Object.assign(new Error('Attachment preview failed: file does not exist.'), { code })
  })

  if (lstat.isSymbolicLink()) {
    throw Object.assign(new Error('Attachment preview failed: symbolic links are not allowed.'), { code: 'symlink' })
  }

  const { resolvedPath, stat } = await resolveReadableFileForIpc(requestedPath, {
    fs: fsImpl,
    maxBytes: ATTACHMENT_PREVIEW_MAX_BYTES,
    purpose: 'Attachment preview'
  })

  const bytes = await fsImpl.promises.readFile(resolvedPath)

  return {
    byteSize: stat.size,
    dataUrl: `data:${spec.mimeType};base64,${bytes.toString('base64')}`,
    mimeType: spec.mimeType
  }
}
