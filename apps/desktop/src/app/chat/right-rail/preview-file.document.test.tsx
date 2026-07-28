import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { strToU8, zipSync } from 'fflate'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $connection } from '@/store/session'

import { LocalFilePreview } from './preview-file'

function simpleDocx(text: string): Uint8Array {
  const files = {
    '[Content_Types].xml': strToU8(
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
        '<Default Extension="xml" ContentType="application/xml"/>' +
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>' +
        '</Types>'
    ),
    '_rels/.rels': strToU8(
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>' +
        '</Relationships>'
    ),
    'word/document.xml': strToU8(
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">' +
        `<w:body><w:p><w:r><w:t>${text}</w:t></w:r></w:p></w:body></w:document>`
    )
  }

  return zipSync(files)
}

afterEach(() => {
  cleanup()
  $connection.set(null)
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('LocalFilePreview documents', () => {
  it('renders a binary, large DOCX as sanitized readable HTML', async () => {
    const bytes = simpleDocx('Side-panel document')
    vi.stubGlobal('fetch', vi.fn(async () => new Response(bytes as BodyInit)))

    render(
      <LocalFilePreview
        reloadKey={0}
        target={{
          binary: true,
          kind: 'file',
          label: 'report.docx',
          large: true,
          path: '/tmp/report.docx',
          previewKind: 'docx',
          source: '/tmp/report.docx',
          url: 'file:///tmp/report.docx'
        }}
      />
    )

    expect(await screen.findByText('Side-panel document')).toBeTruthy()
    expect(fetch).toHaveBeenCalledWith('hermes-media://stream/%2Ftmp%2Freport.docx')
  })

  it('renders a binary, large PDF in the streamed embedded reader', async () => {
    const { container } = render(
      <LocalFilePreview
        reloadKey={0}
        target={{
          binary: true,
          kind: 'file',
          label: 'paper.pdf',
          large: true,
          path: '/tmp/paper.pdf',
          previewKind: 'pdf',
          source: '/tmp/paper.pdf',
          url: 'file:///tmp/paper.pdf'
        }}
      />
    )

    await waitFor(() =>
      expect(container.querySelector('iframe')?.getAttribute('src')).toBe(
        'hermes-media://stream/%2Ftmp%2Fpaper.pdf'
      )
    )
    expect(screen.getByTitle('paper.pdf')).toBeTruthy()
  })

  it('fetches remote PDFs through the authenticated gateway and embeds a blob URL', async () => {
    const createObjectURL = vi.fn(() => 'blob:remote-pdf')
    const fetchFile = vi.fn(async () => new Response(strToU8('%PDF-1.4\n%%EOF') as BodyInit))
    $connection.set({ baseUrl: 'https://gateway.test', mode: 'remote', token: 'secret token' } as never)
    vi.stubGlobal('fetch', fetchFile)
    vi.spyOn(URL, 'createObjectURL').mockImplementation(createObjectURL)
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(vi.fn())

    render(
      <LocalFilePreview
        reloadKey={0}
        target={{
          binary: true,
          kind: 'file',
          label: 'remote.pdf',
          large: true,
          path: '/remote/remote.pdf',
          previewKind: 'pdf',
          source: '/remote/remote.pdf',
          url: 'file:///remote/remote.pdf'
        }}
      />
    )

    expect((await screen.findByTitle('remote.pdf')).getAttribute('src')).toBe('blob:remote-pdf')
    expect(fetchFile).toHaveBeenCalledWith(
      'https://gateway.test/api/files/download?path=%2Fremote%2Fremote.pdf&token=secret%20token'
    )
  })
})
