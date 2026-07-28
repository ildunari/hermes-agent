import { describe, expect, it } from 'vitest'

import { isVisualDocumentPath, localPreviewTarget } from './local-preview'

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
})
