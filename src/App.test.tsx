import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DatasetDetail, DatasetSummary, FrameSample } from '../shared/types'
import App from './App'

function makeFrame(timestamp: number, x: number): FrameSample {
  return {
    timestamp,
    timestampMs: timestamp * 1000,
    dateTimeLocal: '2026-01-01 10:00:00.000',
    pose: { x, y: 2, yaw: 30, valid: true, poseAgeSeconds: 0.01 },
    velocity: {
      vx: 0.5,
      vy: 0.1,
      wz: 0.2,
      linearVelocity: 0.5,
      angularVelocity: 0.2,
      source: 'odom',
      valid: true,
    },
    grid: {
      valid: true,
      filename: `${timestamp * 1000}.png`,
      width: 200,
      height: 200,
      resolution: 0.05,
      originX: -5,
      originY: -5,
      frameId: 'body',
    },
  }
}

const summary: DatasetSummary = {
  id: 'example',
  taskId: 'at-example',
  title: '测试任务',
  robotId: 'Robot-1',
  startedAtLocal: '2026-01-01 10:00:00',
  endedAtLocal: '2026-01-01 10:04:59',
  startedTimestamp: 100,
  endedTimestamp: 399,
  cameraIds: ['cam0', 'cam1', 'cam2', 'cam3', 'cam7'],
  frameCount: 3,
  complete: true,
  warningCount: 0,
}

const detail: DatasetDetail = {
  ...summary,
  timeline: { from: 100, to: 399, duration: 299 },
  cameras: ['cam0', 'cam1', 'cam2', 'cam3', 'cam7'].map((id) => ({
    id,
    positionZh: id,
    positionEn: id,
    videoUrl: `/media/example/cameras/${id}`,
    available: true,
    coverage: 1,
    coverageWarning: '',
  })),
  frames: [makeFrame(101.1, 1), makeFrame(250, 7.25), makeFrame(391.98, 9)],
  parseWarnings: [],
}

class ResizeObserverMock {
  observe() {}
  disconnect() {}
}

describe('App timeline integration', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', ResizeObserverMock)
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue()
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        const body = url === '/api/datasets' ? [summary] : detail
        return { ok: true, json: async () => body } as Response
      }),
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('seeks all videos and switches telemetry at the start, middle and stale tail', async () => {
    render(<App />)
    const slider = await screen.findByRole('slider', { name: '播放进度' })
    const videos = [...document.querySelectorAll('video')]
    expect(videos).toHaveLength(5)
    expect(
      [...document.querySelectorAll('.camera-tile')].map(
        (tile) => tile.querySelector('video')?.dataset.camera ?? null,
      ),
    ).toEqual([null, 'cam3', 'cam7', 'cam0', 'cam2', 'cam1'])
    expect(screen.getByText('无对应数据')).toBeInTheDocument()

    fireEvent.change(slider, { target: { value: '150' } })
    await waitFor(() => expect(screen.getByText('7.25')).toBeInTheDocument())
    expect(videos.every((video) => video.currentTime === 150)).toBe(true)
    expect(document.querySelector('.occupancy-stage img')?.getAttribute('src')).toContain('250000.png')

    fireEvent.change(slider, { target: { value: '0' } })
    await waitFor(() => expect(screen.getByText('无对应数据')).toBeInTheDocument())
    expect(videos.every((video) => video.currentTime === 0)).toBe(true)

    fireEvent.change(slider, { target: { value: '293' } })
    await waitFor(() => expect(screen.getByText('无对应数据')).toBeInTheDocument())
    expect(videos.every((video) => video.currentTime === 293)).toBe(true)
    expect(document.querySelector('.occupancy-stage img')).toBeNull()
  })
})
