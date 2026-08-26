const GRID_BATCH_MAGIC = 0x31424756 // "VGB1" in little-endian order
const HEADER_BYTES = 8
const RECORD_HEADER_BYTES = 8

export const MAX_GRID_BATCH_FRAMES = 64

export interface GridBatchRecord {
  filename: string
  bytes: Uint8Array
}

export function encodeGridBatch(records: GridBatchRecord[]): Uint8Array {
  if (records.length > MAX_GRID_BATCH_FRAMES) throw new Error('占据图批次过大')
  const encoder = new TextEncoder()
  const encoded = records.map((record) => ({
    filename: encoder.encode(record.filename),
    bytes: record.bytes,
  }))
  const totalBytes = encoded.reduce(
    (total, record) => total + RECORD_HEADER_BYTES + record.filename.byteLength + record.bytes.byteLength,
    HEADER_BYTES,
  )
  const output = new Uint8Array(totalBytes)
  const view = new DataView(output.buffer)
  view.setUint32(0, GRID_BATCH_MAGIC, true)
  view.setUint32(4, records.length, true)
  let offset = HEADER_BYTES
  for (const record of encoded) {
    view.setUint32(offset, record.filename.byteLength, true)
    view.setUint32(offset + 4, record.bytes.byteLength, true)
    offset += RECORD_HEADER_BYTES
    output.set(record.filename, offset)
    offset += record.filename.byteLength
    output.set(record.bytes, offset)
    offset += record.bytes.byteLength
  }
  return output
}

export function decodeGridBatch(buffer: ArrayBufferLike): GridBatchRecord[] {
  if (buffer.byteLength < HEADER_BYTES) throw new Error('占据图批次响应不完整')
  const view = new DataView(buffer)
  if (view.getUint32(0, true) !== GRID_BATCH_MAGIC) throw new Error('占据图批次协议不匹配')
  const count = view.getUint32(4, true)
  if (count > MAX_GRID_BATCH_FRAMES) throw new Error('占据图批次帧数非法')
  const decoder = new TextDecoder()
  const records: GridBatchRecord[] = []
  let offset = HEADER_BYTES
  for (let index = 0; index < count; index += 1) {
    if (offset + RECORD_HEADER_BYTES > buffer.byteLength) throw new Error('占据图批次记录不完整')
    const filenameBytes = view.getUint32(offset, true)
    const payloadBytes = view.getUint32(offset + 4, true)
    offset += RECORD_HEADER_BYTES
    const recordEnd = offset + filenameBytes + payloadBytes
    if (recordEnd > buffer.byteLength) throw new Error('占据图批次内容不完整')
    const filename = decoder.decode(new Uint8Array(buffer, offset, filenameBytes))
    offset += filenameBytes
    const bytes = new Uint8Array(buffer, offset, payloadBytes)
    offset += payloadBytes
    records.push({ filename, bytes })
  }
  if (offset !== buffer.byteLength) throw new Error('占据图批次包含多余内容')
  return records
}
