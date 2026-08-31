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

async function readStableAttachment(
  requestedPath: string,
  fsImpl: typeof fs,
  expectedStat: fs.Stats
): Promise<{ bytes: Buffer; byteSize: number }> {
  const noFollow = Number(fs.constants.O_NOFOLLOW || 0)
  let handle: fs.promises.FileHandle

  try {
    // O_NOFOLLOW closes the lstat/open race for the final path component on
    // platforms that support it. Windows still gets the explicit lstat check.
    handle = await fsImpl.promises.open(requestedPath, fs.constants.O_RDONLY | noFollow)
  } catch (error) {
    const code = error && typeof error === 'object' && 'code' in error ? String(error.code) : 'read-error'
    throw Object.assign(new Error('Attachment preview failed: file could not be opened safely.'), {
      code: code === 'ELOOP' ? 'symlink' : code
    })
  }

  try {
    const stat = await handle.stat()

    if (stat.dev !== expectedStat.dev || stat.ino !== expectedStat.ino) {
      throw Object.assign(new Error('Attachment preview failed: file changed before it could be opened.'), {
        code: 'stale-file'
      })
    }

    if (!stat.isFile()) {
      throw Object.assign(new Error('Attachment preview failed: only regular files can be read.'), { code: 'EINVAL' })
    }

    if (stat.size > ATTACHMENT_PREVIEW_MAX_BYTES) {
      throw Object.assign(
        new Error(
          `Attachment preview failed: file is too large (${stat.size} bytes; limit ${ATTACHMENT_PREVIEW_MAX_BYTES} bytes).`
        ),
        { code: 'EFBIG' }
      )
    }

    // Read at most the approved size plus one byte. That extra byte detects a
    // file that grows after fstat without ever allowing an unbounded read into
    // Electron memory. A shrink is stale too; callers should retry a fresh
    // attachment rather than previewing a torn snapshot.
    const buffer = Buffer.allocUnsafe(stat.size + 1)
    let offset = 0

    while (offset < buffer.length) {
      const { bytesRead } = await handle.read(buffer, offset, buffer.length - offset, offset)

      if (bytesRead === 0) {
        break
      }

      offset += bytesRead
    }

    if (offset > ATTACHMENT_PREVIEW_MAX_BYTES) {
      throw Object.assign(new Error('Attachment preview failed: file grew beyond the size limit while reading.'), {
        code: 'EFBIG'
      })
    }

    if (offset !== stat.size) {
      throw Object.assign(new Error('Attachment preview failed: file changed while it was being read.'), {
        code: 'stale-file'
      })
    }

    return { byteSize: offset, bytes: buffer.subarray(0, offset) }
  } finally {
    await handle.close()
  }
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

  const { bytes, byteSize } = await readStableAttachment(resolvedPath, fsImpl, stat)

  return {
    byteSize,
    dataUrl: `data:${spec.mimeType};base64,${bytes.toString('base64')}`,
    mimeType: spec.mimeType
  }
}
