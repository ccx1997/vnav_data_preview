import { describe, expect, it } from 'vitest'
import type { FrameSample } from '../../shared/types'
import {
  computeTrajectoryViewport,
  findFrameIndexAtOrBefore,
  findFreshFrame,
  formatClock,
  formatFixed,
  getTrajectoryFrames,
} from './playback'

function makeFrame(timestamp: number, x: number, y: number, valid = true): FrameSample {
  return {
    timestamp,
    timestampMs: timestamp * 1000,
    dateTimeLocal: '',
    pose: { x, y, yaw: 0, valid, poseAgeSeconds: 0 },
    velocity: {
      vx: 0,
      vy: 0,
      wz: 0,
      linearVelocity: 0,
      angularVelocity: 0,
      source: 'odom',
      valid: true,
    },
    grid: {
      valid: true,
      filename: `${timestamp}.png`,
      width: 200,
      height: 200,
      resolution: 0.05,
      originX: -5,
      originY: -5,
      frameId: 'body',
    },
  }
}

describe('playback helpers', () => {
  const frames = [makeFrame(10, 0, 0), makeFrame(11, 1, 0), makeFrame(13, 3, 2)]

  it('formats values, invalid data, negative zero and clock time', () => {
    expect(formatFixed(1.236)).toBe('1.24')
    expect(formatFixed(-0.001)).toBe('0.00')
    expect(formatFixed(1, false)).toBe('—')
    expect(formatFixed(null)).toBe('—')
    expect(formatClock(299)).toBe('4:59')
  })

  it('finds the latest frame that does not come from the future', () => {
    expect(findFrameIndexAtOrBefore(frames, 9)).toBe(-1)
    expect(findFrameIndexAtOrBefore(frames, 10)).toBe(0)
    expect(findFrameIndexAtOrBefore(frames, 12.9)).toBe(1)
    expect(findFrameIndexAtOrBefore(frames, 99)).toBe(2)
  })

  it('rejects stale frames after the configured age', () => {
    expect(findFreshFrame(frames, 11.5, 1)?.frame.timestamp).toBe(11)
    expect(findFreshFrame(frames, 12.1, 1)).toBeNull()
    expect(findFreshFrame(frames, 9.9, 1)).toBeNull()
  })

  it('keeps only valid poses in the trailing window', () => {
    const history = [makeFrame(50, 0, 0), makeFrame(75, 1, 1), makeFrame(80, 2, 2, false), makeFrame(100, 3, 3)]
    expect(getTrajectoryFrames(history, 100, 30).map((frame) => frame.timestamp)).toEqual([75, 100])
  })

  it('uses a two metre minimum view and preserves the canvas aspect ratio', () => {
    const current = makeFrame(2, 0, 0)
    expect(computeTrajectoryViewport([current], current, 2)).toEqual({ halfHeight: 1, halfWidth: 2 })
    const viewport = computeTrajectoryViewport([makeFrame(1, -5, 0), current], current, 2)
    expect(viewport.halfWidth).toBeCloseTo(6)
    expect(viewport.halfHeight).toBeCloseTo(3)
  })
})
