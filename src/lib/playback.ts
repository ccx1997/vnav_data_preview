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

export function findNearestFrame(
  frames: FrameSample[],
  timestamp: number,
  maximumTimeDifferenceSeconds = 1,
): { frame: FrameSample; index: number } | null {
  const previousIndex = findFrameIndexAtOrBefore(frames, timestamp)
  const index = [previousIndex, previousIndex + 1]
    .filter((candidate) => candidate >= 0 && candidate < frames.length)
    .sort((left, right) => {
      const difference = Math.abs(frames[left].timestamp - timestamp) - Math.abs(frames[right].timestamp - timestamp)
      return difference || left - right
    })[0]
  if (index === undefined || Math.abs(frames[index].timestamp - timestamp) > maximumTimeDifferenceSeconds) return null
  return { frame: frames[index], index }
}

interface FrameMatchOptions {
  maximumTimeDifferenceSeconds?: number
  maximumPositionDifferenceMeters?: number
  maximumYawDifferenceDegrees?: number
  maximumPoseNeighborGapSeconds?: number
}

function yawDifferenceDegrees(left: number, right: number): number {
  return Math.abs((((left - right) % 360) + 540) % 360 - 180)
}

function poseChangeAtTimestamp(
  frames: FrameSample[],
  frameIndex: number,
  timestamp: number,
  maximumNeighborGapSeconds: number,
): { positionMeters: number; yawDegrees: number } | null {
  const frame = frames[frameIndex]
  const { x, y, yaw, valid } = frame.pose
  if (!valid || x === null || y === null || yaw === null) return null
  if (timestamp === frame.timestamp) return { positionMeters: 0, yawDegrees: 0 }

  const neighbors = [frames[frameIndex - 1], frames[frameIndex + 1]]
    .filter((neighbor): neighbor is FrameSample => Boolean(neighbor?.pose.valid))
    .filter((neighbor) =>
      neighbor.pose.x !== null &&
      neighbor.pose.y !== null &&
      neighbor.pose.yaw !== null &&
      Math.abs(neighbor.timestamp - frame.timestamp) <= maximumNeighborGapSeconds,
    )
    .sort((left, right) =>
      Math.abs(left.timestamp - frame.timestamp) - Math.abs(right.timestamp - frame.timestamp),
    )
  const neighbor = neighbors[0]
  if (!neighbor) return null

  const timeScale = Math.abs(
    (timestamp - frame.timestamp) / (neighbor.timestamp - frame.timestamp),
  )
  return {
    positionMeters: Math.hypot(neighbor.pose.x! - x, neighbor.pose.y! - y) * timeScale,
    yawDegrees: yawDifferenceDegrees(neighbor.pose.yaw!, yaw) * timeScale,
  }
}

export function findSynchronizedFrame(
  frames: FrameSample[],
  timestamp: number,
  {
    maximumTimeDifferenceSeconds = 0.05,
    maximumPositionDifferenceMeters = 0.05,
    maximumYawDifferenceDegrees = 3,
    maximumPoseNeighborGapSeconds = 0.5,
  }: FrameMatchOptions = {},
): { frame: FrameSample; index: number } | null {
  const previousIndex = findFrameIndexAtOrBefore(frames, timestamp)
  const candidates = [previousIndex, previousIndex + 1]
    .filter((index) => index >= 0 && index < frames.length)
    .sort((left, right) => {
      const difference = Math.abs(frames[left].timestamp - timestamp) - Math.abs(frames[right].timestamp - timestamp)
      return difference || left - right
    })

  for (const index of candidates) {
    if (Math.abs(frames[index].timestamp - timestamp) > maximumTimeDifferenceSeconds) continue
    const change = poseChangeAtTimestamp(frames, index, timestamp, maximumPoseNeighborGapSeconds)
    if (
      change &&
      change.positionMeters <= maximumPositionDifferenceMeters &&
      change.yawDegrees <= maximumYawDifferenceDegrees
    ) {
      return { frame: frames[index], index }
    }
  }
  return null
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
