import type { AnnotationRect } from './browser-annotation-reporter'

const MAX_BITMAP_DIMENSION = 32_768
const DIGIT_WIDTH = 3
const DIGIT_HEIGHT = 5
const GLYPHS: Readonly<Record<string, readonly string[]>> = Object.freeze({
  '0': ['111', '101', '101', '101', '111'],
  '1': ['010', '110', '010', '010', '111'],
  '2': ['111', '001', '111', '100', '111'],
  '3': ['111', '001', '111', '001', '111'],
  '4': ['101', '101', '111', '001', '001'],
  '5': ['111', '100', '111', '001', '111'],
  '6': ['111', '100', '111', '101', '111'],
  '7': ['111', '001', '001', '001', '001'],
  '8': ['111', '101', '111', '101', '111'],
  '9': ['111', '101', '111', '001', '111']
})

export interface AnnotationScreenshotMarker {
  externalLabel: number
  rects: readonly AnnotationRect[]
}

export interface AnnotationScreenshotViewport {
  height: number
  width: number
}

interface AnnotationBitmap {
  data: Buffer
  height: number
  width: number
}

function validViewport(viewport: AnnotationScreenshotViewport): boolean {
  return Number.isFinite(viewport.width) && Number.isFinite(viewport.height) && viewport.width > 0 && viewport.height > 0
}

function setPixel(bitmap: AnnotationBitmap, x: number, y: number, color: readonly [number, number, number, number]) {
  if (x < 0 || y < 0 || x >= bitmap.width || y >= bitmap.height) {return}
  const offset = (y * bitmap.width + x) * 4
  bitmap.data[offset] = color[2]
  bitmap.data[offset + 1] = color[1]
  bitmap.data[offset + 2] = color[0]
  bitmap.data[offset + 3] = color[3]
}

function fillRoundedRect(
  bitmap: AnnotationBitmap,
  left: number,
  top: number,
  width: number,
  height: number,
  radius: number,
  color: readonly [number, number, number, number]
) {
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const dx = Math.max(radius - x, 0, x - (width - radius - 1))
      const dy = Math.max(radius - y, 0, y - (height - radius - 1))
      if (dx * dx + dy * dy <= radius * radius) {setPixel(bitmap, left + x, top + y, color)}
    }
  }
}

/** Paints compact monotonic numeric labels directly into trusted-main BGRA pixels. */
export function renderAnnotationScreenshotLabels(
  bitmap: AnnotationBitmap,
  viewport: AnnotationScreenshotViewport,
  markers: readonly AnnotationScreenshotMarker[]
): Buffer | null {
  if (
    !Buffer.isBuffer(bitmap.data) || !Number.isSafeInteger(bitmap.width) || !Number.isSafeInteger(bitmap.height) ||
    bitmap.width <= 0 || bitmap.height <= 0 || bitmap.width > MAX_BITMAP_DIMENSION || bitmap.height > MAX_BITMAP_DIMENSION ||
    bitmap.data.byteLength !== bitmap.width * bitmap.height * 4 || !validViewport(viewport)
  ) {return null}

  const output = Buffer.from(bitmap.data)
  const target = { ...bitmap, data: output }
  const scaleX = bitmap.width / viewport.width
  const scaleY = bitmap.height / viewport.height
  const unit = Math.max(1, Math.min(4, Math.round(Math.min(scaleX, scaleY) * 2)))
  const padding = unit * 2
  const gap = unit
  const badgeHeight = DIGIT_HEIGHT * unit + padding * 2

  for (const marker of markers) {
    if (!Number.isSafeInteger(marker.externalLabel) || marker.externalLabel < 1 || !Array.isArray(marker.rects)) {continue}
    const rect = marker.rects[0]
    if (!rect || ![rect.x, rect.y, rect.width, rect.height].every(Number.isFinite)) {continue}
    const digits = String(marker.externalLabel)
    const badgeWidth = digits.length * DIGIT_WIDTH * unit + (digits.length - 1) * gap + padding * 2
    const anchorX = Math.round((rect.x + 2) * scaleX)
    const anchorY = Math.round((rect.y + 2) * scaleY)
    const left = Math.max(0, Math.min(bitmap.width - badgeWidth, anchorX))
    const top = Math.max(0, Math.min(bitmap.height - badgeHeight, anchorY))

    fillRoundedRect(target, left, top, badgeWidth, badgeHeight, unit * 2, [20, 20, 22, 255])
    digits.split('').forEach((digit, digitIndex) => {
      const glyph = GLYPHS[digit]
      glyph?.forEach((row, rowIndex) => {
        row.split('').forEach((pixel, columnIndex) => {
          if (pixel !== '1') {return}
          const x = left + padding + digitIndex * (DIGIT_WIDTH * unit + gap) + columnIndex * unit
          const y = top + padding + rowIndex * unit
          for (let py = 0; py < unit; py += 1) {
            for (let px = 0; px < unit; px += 1) {setPixel(target, x + px, y + py, [255, 255, 255, 255])}
          }
        })
      })
    })
  }

  return output
}

export const ANNOTATION_SCREENSHOT_LIMITS = Object.freeze({ maxBitmapDimension: MAX_BITMAP_DIMENSION })
