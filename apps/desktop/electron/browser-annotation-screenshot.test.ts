import { describe, expect, it } from 'vitest'

import { renderAnnotationScreenshotLabels } from './browser-annotation-screenshot'

function pixel(buffer: Buffer, width: number, x: number, y: number) {
  const offset = (y * width + x) * 4

  return [...buffer.subarray(offset, offset + 4)]
}

describe('trusted-main annotation screenshot labels', () => {
  it('paints a monotonic numeric badge into a copied BGRA bitmap', () => {
    const source = Buffer.alloc(80 * 50 * 4, 127)
    const output = renderAnnotationScreenshotLabels(
      { data: source, height: 50, width: 80 },
      { height: 50, width: 80 },
      [{ externalLabel: 12, rects: [{ height: 10, width: 20, x: 5, y: 6 }] }]
    )

    expect(output).not.toBeNull()
    expect(output).not.toBe(source)
    expect(source.every(value => value === 127)).toBe(true)
    expect(pixel(output!, 80, 12, 12)[3]).toBe(255)
    expect(output!.some((value, index) => index % 4 !== 3 && value === 255)).toBe(true)
  })

  it('scales CSS coordinates to screenshot pixels and clamps edge badges', () => {
    const output = renderAnnotationScreenshotLabels(
      { data: Buffer.alloc(200 * 100 * 4), height: 100, width: 200 },
      { height: 50, width: 100 },
      [{ externalLabel: 9, rects: [{ height: 5, width: 5, x: 99, y: 49 }] }]
    )

    expect(output).not.toBeNull()
    const changedNearEdge = Array.from({ length: 50 }, (_, y) =>
      Array.from({ length: 50 }, (_, x) => pixel(output!, 200, 150 + x, 50 + y))
    ).flat().some(value => value[3] === 255)

    expect(changedNearEdge).toBe(true)
  })

  it('rejects malformed bitmap dimensions without touching pixels', () => {
    expect(renderAnnotationScreenshotLabels(
      { data: Buffer.alloc(4), height: 2, width: 2 },
      { height: 2, width: 2 },
      []
    )).toBeNull()
  })
})
