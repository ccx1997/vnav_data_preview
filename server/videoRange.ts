import { createReadStream, statSync } from 'node:fs'
import type { Response } from 'express'

export function sendVideoWithRange(response: Response, videoPath: string, rangeHeader?: string): void {
  const stat = statSync(videoPath)
  const total = stat.size
  response.setHeader('Accept-Ranges', 'bytes')
  response.setHeader('Content-Type', 'video/mp4')
  response.setHeader('Cache-Control', 'no-store')

  if (!rangeHeader) {
    response.setHeader('Content-Length', total)
    createReadStream(videoPath).pipe(response)
    return
  }

  const byteRange = resolveByteRange(rangeHeader, total)
  if (!byteRange) {
    response.status(416).setHeader('Content-Range', `bytes */${total}`).end()
    return
  }
  const { start, end } = byteRange

  response.status(206)
  response.setHeader('Content-Range', `bytes ${start}-${end}/${total}`)
  response.setHeader('Content-Length', end - start + 1)
  createReadStream(videoPath, { start, end }).pipe(response)
}

export function resolveByteRange(rangeHeader: string, total: number): { start: number; end: number } | null {
  const match = /^bytes=(\d*)-(\d*)$/.exec(rangeHeader)
  if (!match || (!match[1] && !match[2]) || total <= 0) return null

  if (!match[1]) {
    const suffixLength = Number(match[2])
    if (!Number.isFinite(suffixLength) || suffixLength <= 0) return null
    return { start: Math.max(0, total - suffixLength), end: total - 1 }
  }

  const start = Number(match[1])
  const end = match[2] ? Math.min(Number(match[2]), total - 1) : total - 1
  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || start > end || start >= total) return null
  return { start, end }
}
