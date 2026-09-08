import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { encodeGridBatch } from '../shared/gridBatch'
import type { DatasetDetail, DatasetSummary, FrameSample } from '../shared/types'
import App from './App'

const originalCreateObjectUrl = URL.createObjectURL
const originalRevokeObjectUrl = URL.revokeObjectURL
const originalImageDecode = HTMLImageElement.prototype.decode

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

function deferred<T>() {
  let resolvePromise!: (value: T) => void
  const promise = new Promise<T>((resolve) => {
    resolvePromise = resolve
  })
  return { promise, resolve: resolvePromise }
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
  mapName: null,
  timeline: { from: 100, to: 399, duration: 299 },
  cameras: Object.entries({
    cam0: { positionZh: '左', positionEn: 'left' },
    cam1: { positionZh: '右', positionEn: 'right' },
    cam2: { positionZh: '前下', positionEn: 'front_bottom' },
    cam3: { positionZh: '前上', positionEn: 'front_top' },
    cam7: { positionZh: '后上', positionEn: 'rear_top' },
  }).map(([id, position]) => ({
    id,
    ...position,
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
    if (originalCreateObjectUrl) Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: originalCreateObjectUrl })
    else delete (URL as { createObjectURL?: typeof URL.createObjectURL }).createObjectURL
    if (originalRevokeObjectUrl) Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: originalRevokeObjectUrl })
    else delete (URL as { revokeObjectURL?: typeof URL.revokeObjectURL }).revokeObjectURL
    if (originalImageDecode) Object.defineProperty(HTMLImageElement.prototype, 'decode', { configurable: true, value: originalImageDecode })
    else delete (HTMLImageElement.prototype as { decode?: typeof HTMLImageElement.prototype.decode }).decode
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

  it('previews telemetry and the nearest valid occupancy grid independently', async () => {
    const invalidGridFrame = makeFrame(250, 7.25)
    invalidGridFrame.grid = { ...invalidGridFrame.grid, valid: false, filename: null }
    const independentDetail = {
      ...detail,
      frames: [makeFrame(249.6, 6), invalidGridFrame, makeFrame(250.4, 8)],
    }
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => ({
      ok: true,
      json: async () => String(input) === '/api/datasets' ? [summary] : independentDetail,
    }) as Response)

    render(<App />)
    const slider = await screen.findByRole('slider', { name: '播放进度' })
    fireEvent.change(slider, { target: { value: '150' } })

    await waitFor(() => expect(screen.getByText('7.25')).toBeInTheDocument())
    expect(document.querySelector('.occupancy-stage img')?.getAttribute('src')).toContain('249600.png')
  })

  it('uses a reserved GPU preview session only after playback starts', async () => {
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/preview/sessions' && init?.method === 'POST') {
        return {
          ok: true,
          json: async () => ({
            enabled: true,
            mode: 'nvenc',
            sessionId: 'preview-session',
            gpuIndex: 2,
            reason: '空闲',
            profile: { width: 640, fps: 10, bitrateKbps: 450 },
          }),
        } as Response
      }
      return { ok: true, json: async () => url === '/api/datasets' ? [summary] : detail } as Response
    })

    render(<App />)
    expect(await screen.findByText('原始码率')).toBeInTheDocument()
    expect([...document.querySelectorAll('video')].every((video) => !video.src.includes('/preview?'))).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: '播放' }))
    await waitFor(() => expect(document.querySelector('video')?.src).toContain('/preview?session=preview-session'))
    expect(screen.getByText('GPU 2 · 640px/10fps')).toBeInTheDocument()
  })

  it('pauses every camera while one stream buffers and resumes them together', async () => {
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/preview/sessions' && init?.method === 'POST') {
        return {
          ok: true,
          json: async () => ({
            enabled: false,
            mode: 'original',
            sessionId: null,
            gpuIndex: null,
            reason: 'GPU 正忙或资源不足，使用原始视频',
            profile: null,
          }),
        } as Response
      }
      return { ok: true, json: async () => url === '/api/datasets' ? [summary] : detail } as Response
    })

    render(<App />)
    await screen.findByRole('button', { name: '播放' })
    const videos = [...document.querySelectorAll('video')]
    fireEvent.click(screen.getByRole('button', { name: '播放' }))
    await waitFor(() => expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(5))

    vi.mocked(HTMLMediaElement.prototype.pause).mockClear()
    vi.mocked(HTMLMediaElement.prototype.play).mockClear()
    fireEvent.waiting(videos[2])
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledTimes(5)

    fireEvent.canPlay(videos[2])
    await waitFor(() => expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(5))
  })

  it('does not start the video clock until the matching occupancy batch is decoded', async () => {
    const gridBatch = deferred<Response>()
    const synchronizedDetail: DatasetDetail = {
      ...detail,
      frames: [makeFrame(100, 0), makeFrame(100.2, 0.1), makeFrame(100.4, 0.2)],
    }
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn((blob: Blob) => `blob:grid-${blob.size}`),
    })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
    Object.defineProperty(HTMLImageElement.prototype, 'decode', {
      configurable: true,
      value: vi.fn().mockResolvedValue(undefined),
    })
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/grids/batch')) return gridBatch.promise
      if (url === '/api/preview/sessions' && init?.method === 'POST') {
        return {
          ok: true,
          json: async () => ({
            enabled: false,
            mode: 'original',
            sessionId: null,
            gpuIndex: null,
            reason: '原始码率',
            profile: null,
          }),
        } as Response
      }
      return { ok: true, json: async () => url === '/api/datasets' ? [summary] : synchronizedDetail } as Response
    })

    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: '播放' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/preview/sessions', expect.objectContaining({ method: 'POST' })))
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled()

    const batchCall = vi.mocked(fetch).mock.calls.find(([input]) => String(input).endsWith('/grids/batch'))
    const filenames = JSON.parse(String(batchCall?.[1]?.body)).filenames as string[]
    const payload = encodeGridBatch(filenames.map((filename) => ({
      filename,
      bytes: new TextEncoder().encode(filename),
    })))
    gridBatch.resolve({ ok: true, arrayBuffer: async () => payload.buffer } as Response)

    await waitFor(() => expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(5))
    expect(screen.getByText('10:00:00.000')).toBeInTheDocument()
  })

  it('asks for confirmation and deletes the selected Meta and video dataset', async () => {
    let deleted = false
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'DELETE') {
        deleted = true
        return { ok: true, json: async () => ({ deletedId: summary.id }) } as Response
      }
      if (url === '/api/datasets') {
        return { ok: true, json: async () => deleted ? [] : [summary] } as Response
      }
      return { ok: true, json: async () => detail } as Response
    })

    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: '删除数据集' }))
    expect(screen.getByRole('dialog', { name: '删除数据集' })).toBeInTheDocument()
    expect(screen.getByText(/全部 Meta（含占据图）和视频文件/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '确认永久删除' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/datasets/example', { method: 'DELETE' }))
    expect(await screen.findByText('暂无可预览的数据集')).toBeInTheDocument()
    expect(screen.getByText(/已删除数据集\s*测试任务\s*的 Meta 和视频/)).toBeInTheDocument()
  })

  it('requires a map selection and confirms the label for every Meta frame', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/datasets/example/map-name' && init?.method === 'POST') {
        return {
          ok: true,
          json: async () => ({ id: summary.id, mapName: 'B10_map', labeledFrames: 3 }),
        } as Response
      }
      return { ok: true, json: async () => url === '/api/datasets' ? [summary] : detail } as Response
    })

    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: '标注地图名称' }))
    expect(screen.getByRole('dialog', { name: '标注地图名称' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认标签' })).toBeDisabled()

    fireEvent.change(screen.getByRole('combobox', { name: '地图名称' }), { target: { value: 'B10_map' } })
    fireEvent.click(screen.getByRole('button', { name: '确认标签' }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/datasets/example/map-name',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ mapName: 'B10_map' }) }),
    ))
    expect(await screen.findByText('地图标签已写入 3 帧：B10_map')).toBeInTheDocument()
    expect(screen.getByText('地图 B10_map')).toBeInTheDocument()
  })

  it('submits a retained time range as start and end removal durations', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'POST') {
        return {
          ok: true,
          json: async () => ({
            id: summary.id,
            from: 105.2,
            to: 396,
            duration: 290.8,
            removedFrames: 2,
            removedGrids: 2,
            trimmedVideos: 5,
            warnings: [],
          }),
        } as Response
      }
      return { ok: true, json: async () => url === '/api/datasets' ? [summary] : detail } as Response
    })

    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: '裁剪数据集片段' }))
    const dialog = screen.getByRole('dialog', { name: '裁剪首尾片段' })
    expect(dialog).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '纯秒' }))
    fireEvent.change(screen.getByRole('spinbutton', { name: '保留起点秒数' }), { target: { value: '5.2' } })
    fireEvent.change(screen.getByRole('spinbutton', { name: '保留终点秒数' }), { target: { value: '296' } })
    expect(screen.getByText('将保留 290.8 秒')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '确认裁剪' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/datasets/example/trim',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ trimStart: 5.2, trimEnd: 3 }) }),
    ))
    await waitFor(() => expect(screen.getByText(/裁剪完成：保留 290.8 秒/)).toBeInTheDocument())
  })

  it('accepts 00:00:24 to 00:02:23 as a retained range for a 00:02:25.3 dataset', async () => {
    const shortDetail: DatasetDetail = {
      ...detail,
      endedTimestamp: 245.3,
      timeline: { from: 100, to: 245.3, duration: 145.3 },
    }
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => ({
      ok: true,
      json: async () => String(input) === '/api/datasets' ? [summary] : shortDetail,
    }) as Response)

    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: '裁剪数据集片段' }))
    fireEvent.change(screen.getByRole('spinbutton', { name: '保留起点秒' }), { target: { value: '24' } })
    fireEvent.change(screen.getByRole('spinbutton', { name: '保留终点分钟' }), { target: { value: '2' } })
    fireEvent.change(screen.getByRole('spinbutton', { name: '保留终点秒' }), { target: { value: '23' } })

    expect(screen.getByText('将保留 00:01:59.0')).toBeInTheDocument()
    expect(screen.getByText('删除开头 24.0 秒 · 结尾 2.3 秒')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认裁剪' })).toBeEnabled()
  })

  it('switches between hour-minute-second and pure-second inputs without changing the duration', async () => {
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: '裁剪数据集片段' }))

    expect(screen.getByRole('button', { name: '时分秒' })).toHaveAttribute('aria-pressed', 'true')
    fireEvent.change(screen.getByRole('spinbutton', { name: '保留起点分钟' }), { target: { value: '1' } })
    fireEvent.change(screen.getByRole('spinbutton', { name: '保留起点秒' }), { target: { value: '5.5' } })
    fireEvent.change(screen.getByRole('spinbutton', { name: '保留终点秒' }), { target: { value: '50' } })
    expect(screen.getByText('当前长度 00:04:59.0')).toBeInTheDocument()
    expect(screen.getByText('将保留 00:03:44.5')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '纯秒' }))
    expect(screen.getByRole('spinbutton', { name: '保留起点秒数' })).toHaveValue(65.5)
    expect(screen.getByRole('spinbutton', { name: '保留终点秒数' })).toHaveValue(290)
    expect(screen.getByText('将保留 224.5 秒')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '时分秒' }))
    expect(screen.getByRole('spinbutton', { name: '保留起点分钟' })).toHaveValue(1)
    expect(screen.getByRole('spinbutton', { name: '保留起点秒' })).toHaveValue(5.5)
    expect(screen.getByRole('spinbutton', { name: '保留终点秒' })).toHaveValue(50)
    fireEvent.change(screen.getByRole('spinbutton', { name: '保留起点分钟' }), { target: { value: '60' } })
    expect(screen.getByText('请输入有效时间')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认裁剪' })).toBeDisabled()
  })

  it('carries rounded seconds into minutes instead of showing an invalid 60-second field', async () => {
    const roundedDetail: DatasetDetail = {
      ...detail,
      endedTimestamp: 159.9998,
      timeline: { from: 100, to: 159.9998, duration: 59.9998 },
    }
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => ({
      ok: true,
      json: async () => String(input) === '/api/datasets' ? [summary] : roundedDetail,
    }) as Response)

    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: '裁剪数据集片段' }))

    expect(screen.getByText('当前长度 00:01:00.0')).toBeInTheDocument()
    expect(screen.getByRole('spinbutton', { name: '保留终点分钟' })).toHaveValue(1)
    expect(screen.getByRole('spinbutton', { name: '保留终点秒' })).toHaveValue(0)
  })
})
