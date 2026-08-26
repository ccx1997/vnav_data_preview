import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { FrameSample } from '../../shared/types'
import { TelemetryPanel } from './TelemetryPanel'

const frame: FrameSample = {
  timestamp: 100,
  timestampMs: 100000,
  dateTimeLocal: '2026-08-14 17:43:51.074',
  pose: { x: -0.193, y: 0.026, yaw: -5.5, valid: true, poseAgeSeconds: 0.03 },
  velocity: {
    vx: 0.123,
    vy: -0.004,
    wz: 0.8,
    linearVelocity: 0.123,
    angularVelocity: 0.8,
    source: 'pose_diff_fallback',
    valid: true,
  },
  grid: {
    valid: true,
    filename: '100000.png',
    width: 200,
    height: 200,
    resolution: 0.05,
    originX: -5,
    originY: -5,
    frameId: 'body',
  },
}

describe('TelemetryPanel', () => {
  it('renders pose and velocity with two decimals and source information', () => {
    render(<TelemetryPanel frame={frame} />)
    expect(screen.getByText('近邻预览')).toBeInTheDocument()
    expect(screen.getByText('-0.19')).toBeInTheDocument()
    expect(screen.getByText('0.03')).toBeInTheDocument()
    expect(screen.getByText('-5.50')).toBeInTheDocument()
    expect(screen.getByText('0.12')).toBeInTheDocument()
    expect(screen.getByText('0.00')).toBeInTheDocument()
    expect(screen.getByText('0.80')).toBeInTheDocument()
    expect(screen.getByText('pose_diff_fallback')).toBeInTheDocument()
  })

  it('shows empty placeholders when no current frame exists', () => {
    render(<TelemetryPanel frame={null} />)
    expect(screen.getByText('无对应数据')).toBeInTheDocument()
    expect(screen.getAllByText('—')).toHaveLength(7)
  })
})
