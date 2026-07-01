import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { readDesktopFileDataUrl, selectLocalDesktopPaths } from '@/lib/desktop-fs'
import { $composerAttachments } from '@/store/composer'

import { type DroppedFile, imageDropPreviewOptions, partitionDroppedFiles, useComposerActions } from './use-composer-actions'

vi.mock('@/lib/desktop-fs', async importOriginal => {
  const actual = await importOriginal<typeof import('@/lib/desktop-fs')>()

  return {
    ...actual,
    readDesktopFileDataUrl: vi.fn(),
    selectLocalDesktopPaths: vi.fn()
  }
})

// A Finder/Explorer drop carries a native File handle; an in-app drag (project
// tree, gutter line ref) is path-only. The split decides whether a drop becomes
// an inline @file: ref (in-app, workspace-relative, gateway-resolvable) or goes
// through the upload pipeline (OS drop — absolute local path a remote gateway
// can't read, plus image bytes for vision).
const osDrop = (path: string): DroppedFile => ({ file: new File(['x'], path.split('/').pop() || 'f'), path })
const inAppRef = (path: string, extra: Partial<DroppedFile> = {}): DroppedFile => ({ path, ...extra })

afterEach(() => {
  $composerAttachments.set([])
  vi.clearAllMocks()
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

describe('partitionDroppedFiles', () => {
  it('routes File-bearing OS drops to osDrops and path-only in-app drags to inAppRefs', () => {
    const finderPdf = osDrop('/Users/mahmoud/Downloads/DEVIS_signed.pdf')
    const projectFile = inAppRef('src/index.ts')

    const { inAppRefs, osDrops } = partitionDroppedFiles([finderPdf, projectFile])

    expect(osDrops).toEqual([finderPdf])
    expect(inAppRefs).toEqual([projectFile])
  })

  it('treats an OS screenshot drop as an upload target (so it gets byte upload + vision)', () => {
    const screenshot = osDrop('/var/folders/tmp/Screenshot 2026-06-09.png')

    const { inAppRefs, osDrops } = partitionDroppedFiles([screenshot])

    expect(osDrops).toEqual([screenshot])
    expect(inAppRefs).toEqual([])
  })

  it('keeps gutter line-range drags inline (no File handle)', () => {
    const lineRef = inAppRef('src/app.ts', { line: 10, lineEnd: 20 })

    const { inAppRefs, osDrops } = partitionDroppedFiles([lineRef])

    expect(osDrops).toEqual([])
    expect(inAppRefs).toEqual([lineRef])
  })

  it('splits a mixed drop and preserves order within each group', () => {
    const a = inAppRef('a.ts')
    const b = osDrop('/abs/b.pdf')
    const c = inAppRef('c.ts')
    const d = osDrop('/abs/d.png')

    const { inAppRefs, osDrops } = partitionDroppedFiles([a, b, c, d])

    expect(inAppRefs).toEqual([a, c])
    expect(osDrops).toEqual([b, d])
  })

  it('returns empty groups for an empty drop', () => {
    expect(partitionDroppedFiles([])).toEqual({ inAppRefs: [], osDrops: [] })
  })

  it('previews File-bearing image drops locally even when Chromium exposes an absolute path', () => {
    const screenshot = osDrop('/Users/kosta/Desktop/Screenshot 2026-06-30.png')

    expect(imageDropPreviewOptions(screenshot)).toEqual({ localPreview: true })
  })

  it('does not force local preview for path-only in-app image drags', () => {
    const remoteTreeImage = inAppRef('/remote/work/image.png')

    expect(imageDropPreviewOptions(remoteTreeImage)).toEqual({})
  })

  it('previews native image picker paths through the local desktop bridge', async () => {
    const readFileDataUrl = vi.fn().mockResolvedValue('data:image/png;base64,cGljaw==')

    window.hermesDesktop = { readFileDataUrl } as never
    vi.mocked(selectLocalDesktopPaths).mockResolvedValue(['/Users/kosta/Desktop/pick.png'])

    const { result } = renderHook(() =>
      useComposerActions({ activeSessionId: null, currentCwd: '/Users/kosta', requestGateway: vi.fn() })
    )

    await act(async () => {
      await result.current.pickImages()
    })

    expect(readFileDataUrl).toHaveBeenCalledWith('/Users/kosta/Desktop/pick.png')
    expect(readDesktopFileDataUrl).not.toHaveBeenCalled()
    expect($composerAttachments.get()).toContainEqual(
      expect.objectContaining({ path: '/Users/kosta/Desktop/pick.png', previewUrl: 'data:image/png;base64,cGljaw==' })
    )
  })
})
