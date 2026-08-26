import { describe, expect, it } from 'vitest'
import { decodeGridBatch, encodeGridBatch, MAX_GRID_BATCH_FRAMES } from './gridBatch'

describe('grid batch protocol', () => {
  it('round-trips filenames and binary PNG payloads', () => {
    const encoded = encodeGridBatch([
      { filename: '100.png', bytes: new Uint8Array([0x89, 0x50, 0x4e, 0x47]) },
      { filename: '200.png', bytes: new Uint8Array([0, 1, 2, 255]) },
    ])

    expect(decodeGridBatch(encoded.buffer)).toEqual([
      { filename: '100.png', bytes: new Uint8Array([0x89, 0x50, 0x4e, 0x47]) },
      { filename: '200.png', bytes: new Uint8Array([0, 1, 2, 255]) },
    ])
  })

  it('rejects oversized and truncated batches', () => {
    expect(() => encodeGridBatch(Array.from({ length: MAX_GRID_BATCH_FRAMES + 1 }, (_, index) => ({
      filename: `${index}.png`,
      bytes: new Uint8Array(),
    })))).toThrow(/批次过大/)

    const encoded = encodeGridBatch([{ filename: '100.png', bytes: new Uint8Array([1, 2, 3]) }])
    expect(() => decodeGridBatch(encoded.buffer.slice(0, -1))).toThrow(/不完整/)
  })
})
