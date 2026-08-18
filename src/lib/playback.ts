import type { FrameSample } from '../../shared/types'

export function formatFixed(value: number | null, valid = true): string {
  if (!valid || value === null || !Number.isFinite(value)) return '—'
  return (Math.abs(value) < 0.005 ? 0 : value).toFixed(2)
}

export function formatClock(seconds: number): string {
  const safeSeconds = Math.max(0, Number.isFinite(seconds) ? seconds : 0)
  const minutes = Math.floor(safeSeconds / 60)
  const remainder = Math.floor(safeSeconds % 60)
  return `${minutes}:${remainder.toString().padStart(2, '0')}`
}

export function findFrameIndexAtOrBefore(frames: FrameSample[], timestamp: number): number {
  let low = 0
  let high = frames.length - 1
  let result = -1

  while (low <= high) {
    const middle = Math.floor((low + high) / 2)
    if (frames[middle].timestamp <= timestamp) {
      result = middle
      low = middle + 1
    } else {
      high = middle - 1
    }
  }
  return result
}

export function findFreshFrame(
  frames: FrameSample[],
  timestamp: number,
  maximumAgeSeconds = 1,
): { frame: FrameSample; index: number } | null {
  const index = findFrameIndexAtOrBefore(frames, timestamp)
  if (index < 0 || timestamp - frames[index].timestamp > maximumAgeSeconds) return null
  return { frame: frames[index], index }
}

export function getTrajectoryFrames(
  frames: FrameSample[],
  currentTimestamp: number,
  windowSeconds = 30,
): FrameSample[] {
  const lastIndex = findFrameIndexAtOrBefore(frames, currentTimestamp)
  if (lastIndex < 0) return []
  const firstTimestamp = currentTimestamp - windowSeconds
  let firstIndex = lastIndex
  while (firstIndex > 0 && frames[firstIndex - 1].timestamp >= firstTimestamp) firstIndex -= 1
  return frames
    .slice(firstIndex, lastIndex + 1)
    .filter(
      (frame) =>
        frame.pose.valid &&
        frame.pose.x !== null &&
        frame.pose.y !== null &&
        frame.timestamp >= firstTimestamp,
    )
}

export interface TrajectoryViewport {
  halfWidth: number
  halfHeight: number
}

export function computeTrajectoryViewport(
  frames: FrameSample[],
  currentFrame: FrameSample,
  aspectRatio: number,
): TrajectoryViewport {
  const currentX = currentFrame.pose.x ?? 0
  const currentY = currentFrame.pose.y ?? 0
  const safeAspect = Math.max(0.1, aspectRatio)
  let maximumX = 0
  let maximumY = 0
  for (const frame of frames) {
    if (frame.pose.x === null || frame.pose.y === null) continue
    maximumX = Math.max(maximumX, Math.abs(frame.pose.x - currentX))
    maximumY = Math.max(maximumY, Math.abs(frame.pose.y - currentY))
  }
  const halfHeight = Math.max(1, maximumY * 1.2, (maximumX * 1.2) / safeAspect)
  return { halfHeight, halfWidth: halfHeight * safeAspect }
}
