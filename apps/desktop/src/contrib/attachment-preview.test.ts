import { describe, expect, it, vi } from 'vitest'

import type { PreviewTarget } from '@/store/preview'

import {
  ATTACHMENT_PREVIEW_MAX_BYTES,
  ATTACHMENT_PREVIEWERS_AREA,
  type AttachmentPreviewerContribution,
  loadAttachmentPreviewSource,
  selectAttachmentPreviewer
} from './attachment-preview'
import { createPluginContext } from './plugin'
import { registry } from './registry'

const previewer: AttachmentPreviewerContribution = {
  extensions: ['.docx', '.pdf'],
  maxBytes: 1024,
  mimeTypes: ['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
  render: () => null
}

function target(path: string, overrides: Partial<PreviewTarget> = {}): PreviewTarget {
  return {
    kind: 'file',
    label: path.split('/').at(-1) || path,
    path,
    source: path,
    url: `file://${path}`,
    ...overrides
  }
}

function contribution(data: AttachmentPreviewerContribution = previewer) {
  return { area: ATTACHMENT_PREVIEWERS_AREA, data, id: 'docs', source: 'plugin:docs' }
}

function dataUrl(mime: string, text: string) {
  return `data:${mime};base64,${btoa(text)}`
}

describe('attachment previewer selection', () => {
  it.each(['/fixtures/report.docx', '/fixtures/paper.pdf'])('selects a matching plugin for %s', path => {
    expect(selectAttachmentPreviewer(target(path), [contribution()])?.contribution.id).toBe('docs')
  })

  it('leaves unsupported attachments to the core fallback', () => {
    expect(selectAttachmentPreviewer(target('/fixtures/archive.zip'), [contribution()])).toBeNull()
  })

  it.each(['/fixtures/report.pdf#notes', '/fixtures/report.pdf?draft'])(
    'treats %s as a literal filesystem path',
    path => {
      expect(selectAttachmentPreviewer(target(path), [contribution()])).toBeNull()
    }
  )

  it('rejects a MIME mismatch before mounting plugin UI', () => {
    expect(
      selectAttachmentPreviewer(target('/fixtures/paper.pdf', { mimeType: 'text/html' }), [contribution()])
    ).toBeNull()
  })

  it('removes a previewer contribution when its plugin unloads', () => {
    const disposers: Array<() => void> = []
    const ctx = createPluginContext('docs-lifecycle', dispose => disposers.push(dispose))
    ctx.register({ area: ATTACHMENT_PREVIEWERS_AREA, data: previewer, id: 'documents' })

    expect(
      selectAttachmentPreviewer(target('/fixtures/paper.pdf'), registry.getArea(ATTACHMENT_PREVIEWERS_AREA))
    ).not.toBeNull()
    disposers.forEach(dispose => dispose())
    expect(
      selectAttachmentPreviewer(target('/fixtures/paper.pdf'), registry.getArea(ATTACHMENT_PREVIEWERS_AREA))
    ).toBeNull()
  })
})

describe('secure attachment preview source', () => {
  it('loads a remote PDF only through the injected authenticated reader', async () => {
    const value = dataUrl('application/pdf', '%PDF-1.4')
    const readRemote = vi.fn(async () => value)

    await expect(
      loadAttachmentPreviewSource(target('/remote/paper.pdf'), previewer, '.pdf', { readRemote, remote: true })
    ).resolves.toEqual({ byteSize: 8, dataUrl: value, mimeType: 'application/pdf' })
    expect(readRemote).toHaveBeenCalledWith('/remote/paper.pdf')
  })

  it.each([
    ['remote URL', target('https://example.test/paper.pdf', { url: 'https://example.test/paper.pdf' })],
    ['traversal', target('/fixtures/../secret.pdf')]
  ])('rejects %s without calling the reader', async (_label, unsafe) => {
    const readRemote = vi.fn()

    await expect(loadAttachmentPreviewSource(unsafe, previewer, '.pdf', { readRemote, remote: true })).rejects.toThrow(
      /rejected/
    )
    expect(readRemote).not.toHaveBeenCalled()
  })

  it('rejects oversized metadata and returned bytes', async () => {
    await expect(
      loadAttachmentPreviewSource(target('/remote/paper.pdf', { byteSize: 1025 }), previewer, '.pdf', {
        readRemote: vi.fn(),
        remote: true
      })
    ).rejects.toThrow(/exceeds/)

    const large = dataUrl('application/pdf', 'x'.repeat(1025))
    await expect(
      loadAttachmentPreviewSource(target('/remote/paper.pdf'), previewer, '.pdf', {
        readRemote: async () => large,
        remote: true
      })
    ).rejects.toThrow(/exceeds/)
  })

  it('rejects an unexpected returned MIME type', async () => {
    await expect(
      loadAttachmentPreviewSource(target('/remote/paper.pdf'), previewer, '.pdf', {
        readRemote: async () => dataUrl('text/html', '<script>'),
        remote: true
      })
    ).rejects.toThrow(/MIME/)
  })

  it('caps plugin-requested limits at the host ceiling', async () => {
    const permissive = { ...previewer, maxBytes: ATTACHMENT_PREVIEW_MAX_BYTES * 2 }
    const tooLarge = target('/remote/paper.pdf', { byteSize: ATTACHMENT_PREVIEW_MAX_BYTES + 1 })

    await expect(
      loadAttachmentPreviewSource(tooLarge, permissive, '.pdf', { readRemote: vi.fn(), remote: true })
    ).rejects.toThrow(String(ATTACHMENT_PREVIEW_MAX_BYTES))
  })
})
