import { describe, expect, it } from 'vitest'

import { isVisualDocumentPath, localPreviewTarget } from './local-preview'
import { normalizeDocumentPreviewKind } from './preview-target'

describe('local document previews', () => {
  it.each([
    ['/tmp/report.docx', 'docx'],
    ['/tmp/report.DOCX', 'docx'],
    ['/tmp/paper.pdf', 'pdf'],
    ['/tmp/paper.PDF?download=1', 'pdf']
  ] as const)('classifies %s as a visual %s preview', (path, previewKind) => {
    expect(isVisualDocumentPath(path)).toBe(true)
    expect(localPreviewTarget(path)?.previewKind).toBe(previewKind)
  })

  it('does not treat unsupported office formats as visual previews', () => {
    expect(isVisualDocumentPath('/tmp/legacy.doc')).toBe(false)
    expect(localPreviewTarget('/tmp/legacy.doc')?.previewKind).toBe('text')
  })

  it('repairs stale binary classification returned by preview IPC', () => {
    const target = {
      binary: true,
      kind: 'file' as const,
      label: 'report.docx',
      path: '/tmp/report.docx',
      previewKind: 'binary' as const,
      source: '/tmp/report.docx',
      url: 'file:///tmp/report.docx'
    }

    expect(normalizeDocumentPreviewKind(target)).toEqual({ ...target, previewKind: 'docx' })
  })
})
