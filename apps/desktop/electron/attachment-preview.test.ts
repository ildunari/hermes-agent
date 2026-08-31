import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

import { ATTACHMENT_PREVIEW_MAX_BYTES, readAttachmentPreviewForIpc } from './attachment-preview'

const fixtureDirs: string[] = []

async function fixtureDir() {
  const dir = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'hermes-attachment-preview-'))
  fixtureDirs.push(dir)

  return dir
}

afterEach(async () => {
  await Promise.all(fixtureDirs.splice(0).map(dir => fs.promises.rm(dir, { force: true, recursive: true })))
})

async function expectCode(promise: Promise<unknown>, code: string) {
  await expect(promise).rejects.toMatchObject({ code })
}

describe('readAttachmentPreviewForIpc', () => {
  it('reads fixture PDF and DOCX files with fixed MIME types', async () => {
    const dir = await fixtureDir()
    const pdf = path.join(dir, 'paper.pdf')
    const docx = path.join(dir, 'report.docx')
    await fs.promises.writeFile(pdf, '%PDF-1.4\n%%EOF')
    await fs.promises.writeFile(docx, Buffer.from('PK\u0003\u0004fixture'))

    const pdfResult = await readAttachmentPreviewForIpc({ path: pdf, type: 'pdf' })
    const docxResult = await readAttachmentPreviewForIpc({ path: docx, type: 'docx' })

    expect(pdfResult.mimeType).toBe('application/pdf')
    expect(pdfResult.dataUrl).toMatch(/^data:application\/pdf;base64,/)
    expect(docxResult.mimeType).toBe('application/vnd.openxmlformats-officedocument.wordprocessingml.document')
  })

  it('rejects unsupported types, extension mismatch, traversal, and remote URLs', async () => {
    const dir = await fixtureDir()
    const pdf = path.join(dir, 'paper.pdf')
    await fs.promises.writeFile(pdf, '%PDF-1.4')

    await expectCode(readAttachmentPreviewForIpc({ path: pdf, type: 'zip' as never }), 'unsupported-type')
    await expectCode(readAttachmentPreviewForIpc({ path: pdf, type: 'docx' }), 'extension-mismatch')
    await expectCode(
      readAttachmentPreviewForIpc({ path: `${dir}${path.sep}..${path.sep}paper.pdf`, type: 'pdf' }),
      'path-traversal'
    )
    await expectCode(readAttachmentPreviewForIpc({ path: 'https://example.test/paper.pdf', type: 'pdf' }), 'invalid-scheme')
  })

  it('rejects symlink, oversized, and stale fixture files', async () => {
    const dir = await fixtureDir()
    const pdf = path.join(dir, 'paper.pdf')
    const link = path.join(dir, 'linked.pdf')
    const large = path.join(dir, 'large.pdf')
    const stale = path.join(dir, 'stale.pdf')
    await fs.promises.writeFile(pdf, '%PDF-1.4')
    await fs.promises.symlink(pdf, link)
    await fs.promises.writeFile(large, Buffer.alloc(ATTACHMENT_PREVIEW_MAX_BYTES + 1))
    await fs.promises.writeFile(stale, '%PDF-1.4')
    await fs.promises.unlink(stale)

    await expectCode(readAttachmentPreviewForIpc({ path: link, type: 'pdf' }), 'symlink')
    await expectCode(readAttachmentPreviewForIpc({ path: large, type: 'pdf' }), 'EFBIG')
    await expectCode(readAttachmentPreviewForIpc({ path: stale, type: 'pdf' }), 'ENOENT')
  })
})
