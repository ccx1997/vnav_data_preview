import { describe, expect, it } from 'vitest'
import type { FrameSample } from '../../shared/types'
import {
  computeTrajectoryViewport,
  findFrameIndexAtOrBefore,
  findNearestFrame,
  findSynchronizedFrame,
  formatClock,
  formatFixed,
  getTrajectoryFrames,
} from './playback'

function makeFrame(timestamp: number, x: number, y: number, valid = true, yaw = 0): FrameSample {
  return {
    timestamp,
    timestampMs: timestamp * 1000,
    dateTimeLocal: '',
    pose: { x, y, yaw, valid, poseAgeSeconds: 0 },
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

  it('finds the nearest preview frame on either side within one second', () => {
    expect(findNearestFrame(frames, 9.2)?.frame.timestamp).toBe(10)
    expect(findNearestFrame(frames, 10.6)?.frame.timestamp).toBe(11)
    expect(findNearestFrame(frames, 12.1)?.frame.timestamp).toBe(13)
    expect(findNearestFrame(frames, 14.1)).toBeNull()
  })

  it('selects the nearest frame within the synchronized time window', () => {
    const samples = [makeFrame(10, 0, 0), makeFrame(10.2, 0.1, 0), makeFrame(10.4, 0.2, 0)]
    expect(findSynchronizedFrame(samples, 10.04)?.frame.timestamp).toBe(10)
    expect(findSynchronizedFrame(samples, 10.17)?.frame.timestamp).toBe(10.2)
    expect(findSynchronizedFrame(samples, 10.06)).toBeNull()
  })

  it('rejects excessive position or yaw changes without using velocity', () => {
    expect(findSynchronizedFrame([makeFrame(20, 0, 0), makeFrame(20.2, 1, 0)], 20.04)).toBeNull()
    expect(findSynchronizedFrame([makeFrame(30, 0, 0, true, 0), makeFrame(30.2, 0, 0, true, 20)], 30.04)).toBeNull()
  })

  it('handles wrapped yaw and rejects unreliable pose neighborhoods', () => {
    expect(
      findSynchronizedFrame([makeFrame(40, 0, 0, true, 179), makeFrame(40.2, 0, 0, true, -179)], 40.04),
    ).not.toBeNull()
    expect(findSynchronizedFrame([makeFrame(50, 0, 0), makeFrame(50.6, 0, 0)], 50.04)).toBeNull()
    expect(findSynchronizedFrame([makeFrame(60, 0, 0)], 60)).not.toBeNull()
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
