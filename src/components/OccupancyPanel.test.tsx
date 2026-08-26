import { StrictMode } from 'react'
import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { encodeGridBatch } from '../../shared/gridBatch'
import type { FrameSample } from '../../shared/types'
import { OccupancyPanel } from './OccupancyPanel'

function makeFrame(timestamp: number): FrameSample {
  return {
    timestamp,
    timestampMs: timestamp * 1000,
    dateTimeLocal: `2026-08-25 10:00:${String(timestamp).padStart(2, '0')}.000`,
    pose: { x: 0, y: 0, yaw: 0, valid: true, poseAgeSeconds: 0 },
    velocity: {
      vx: 0,
      vy: 0,
      wz: 0,
      linearVelocity: 0,
      angularVelocity: 0,
      source: '',
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

function deferred<T>() {
  let resolvePromise!: (value: T) => void
  const promise = new Promise<T>((resolve) => {
    resolvePromise = resolve
  })
  return { promise, resolve: resolvePromise }
}

function batchResponse(filenames: string[]): Response {
  const payload = encodeGridBatch(filenames.map((filename) => ({
    filename,
    bytes: new TextEncoder().encode(filename),
  })))
  return { ok: true, arrayBuffer: async () => payload.buffer } as Response
}

async function flushGridWork() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('OccupancyPanel synchronized batch display', () => {
  const originalCreateObjectUrl = URL.createObjectURL
  const originalRevokeObjectUrl = URL.revokeObjectURL

  beforeEach(() => {
    let nextObjectUrl = 0
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn(() => `blob:grid-${++nextObjectUrl}`),
    })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
    Object.defineProperty(HTMLImageElement.prototype, 'decode', {
      configurable: true,
      value: vi.fn().mockResolvedValue(undefined),
    })
  })

  afterEach(() => {
    if (originalCreateObjectUrl) Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: originalCreateObjectUrl })
    else delete (URL as { createObjectURL?: typeof URL.createObjectURL }).createObjectURL
    if (originalRevokeObjectUrl) Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: originalRevokeObjectUrl })
    else delete (URL as { revokeObjectURL?: typeof URL.revokeObjectURL }).revokeObjectURL
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('loads a future window in one RTT and switches to the exact cached frame', async () => {
    const firstBatch = deferred<Response>()
    const buffering = vi.fn()
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      const filenames = JSON.parse(String(init?.body)).filenames as string[]
      return firstBatch.promise.then(() => batchResponse(filenames))
    })
    vi.stubGlobal('fetch', fetchMock)
    const frames = Array.from({ length: 32 }, (_, index) => makeFrame(index + 1))
    const { container, rerender } = render(
      <OccupancyPanel
        datasetId="example"
        frame={frames[0]}
        frameIndex={0}
        allFrames={frames}
        onBufferingChange={buffering}
      />,
    )

    expect(screen.getByText('正在同步占据图')).toBeInTheDocument()
    expect(container.querySelector('.occupancy-stage img')).toBeNull()
    expect(buffering).toHaveBeenLastCalledWith(true)

    await act(async () => firstBatch.resolve({} as Response))
    await waitFor(() => expect(screen.getByText('10:00:01.000')).toBeInTheDocument())
    const firstSource = container.querySelector('.occupancy-stage img')?.getAttribute('src')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(buffering).toHaveBeenLastCalledWith(false)

    rerender(
      <OccupancyPanel
        datasetId="example"
        frame={frames[1]}
        frameIndex={1}
        allFrames={frames}
        onBufferingChange={buffering}
      />,
    )
    await waitFor(() => expect(screen.getByText('10:00:02.000')).toBeInTheDocument())
    expect(container.querySelector('.occupancy-stage img')?.getAttribute('src')).not.toBe(firstSource)
    expect(screen.queryByText('正在同步占据图')).not.toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('never presents an old grid as the current video timestamp after an uncached seek', async () => {
    const secondBatch = deferred<Response>()
    let requestCount = 0
    vi.stubGlobal('fetch', vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      const filenames = JSON.parse(String(init?.body)).filenames as string[]
      requestCount += 1
      return requestCount === 1 ? Promise.resolve(batchResponse(filenames)) : secondBatch.promise.then(() => batchResponse(filenames))
    }))
    const frames = Array.from({ length: 64 }, (_, index) => makeFrame(index + 1))
    const { container, rerender } = render(
      <OccupancyPanel datasetId="example" frame={frames[0]} frameIndex={0} allFrames={frames} />,
    )
    await waitFor(() => expect(screen.getByText('10:00:01.000')).toBeInTheDocument())

    rerender(<OccupancyPanel datasetId="example" frame={frames[50]} frameIndex={50} allFrames={frames} />)
    expect(container.querySelector('.occupancy-stage img')).toBeNull()
    expect(screen.queryByText('10:00:01.000')).not.toBeInTheDocument()
    expect(screen.getByText('正在同步占据图')).toBeInTheDocument()

    await act(async () => secondBatch.resolve({} as Response))
    await waitFor(() => expect(screen.getByText('10:00:51.000')).toBeInTheDocument())
  })

  it('keeps the decoded batch cache when StrictMode replays the lifecycle effects', async () => {
    const buffering = vi.fn()
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      const filenames = JSON.parse(String(init?.body)).filenames as string[]
      return Promise.resolve(batchResponse(filenames))
    })
    vi.stubGlobal('fetch', fetchMock)
    const frames = Array.from({ length: 32 }, (_, index) => makeFrame(index + 1))
    const view = (frameIndex: number) => (
      <StrictMode>
        <OccupancyPanel
          datasetId="example"
          frame={frames[frameIndex]}
          frameIndex={frameIndex}
          allFrames={frames}
          onBufferingChange={buffering}
        />
      </StrictMode>
    )
    const { container, rerender } = render(view(0))

    await waitFor(() => expect(screen.getByText('10:00:01.000')).toBeInTheDocument())
    await waitFor(() => expect(container.querySelector('.occupancy-stage img')?.getAttribute('src')).toMatch(/^blob:/))
    const requestsAfterInitialLoad = fetchMock.mock.calls.length
    expect(requestsAfterInitialLoad).toBeGreaterThan(0)
    expect(buffering).toHaveBeenLastCalledWith(false)

    rerender(view(1))
    expect(screen.queryByText('正在同步占据图')).not.toBeInTheDocument()
    expect(container.querySelector('.occupancy-stage img')?.getAttribute('src')).toMatch(/^blob:/)
    await waitFor(() => expect(screen.getByText('10:00:02.000')).toBeInTheDocument())
    expect(screen.queryByText('正在同步占据图')).not.toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(requestsAfterInitialLoad)
    expect(buffering).toHaveBeenLastCalledWith(false)
  })

  it('protects forward-prefetched frames while a third batch trims the cache', async () => {
    let clock = 0
    vi.spyOn(Date, 'now').mockImplementation(() => ++clock)
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      const filenames = JSON.parse(String(init?.body)).filenames as string[]
      return Promise.resolve(batchResponse(filenames))
    })
    vi.stubGlobal('fetch', fetchMock)
    const frames = Array.from({ length: 96 }, (_, index) => makeFrame(index + 1))
    const view = (frameIndex: number) => (
      <OccupancyPanel datasetId="example" frame={frames[frameIndex]} frameIndex={frameIndex} allFrames={frames} />
    )
    const { container, rerender } = render(view(0))

    await waitFor(() => expect(container.querySelector('.occupancy-stage img')?.getAttribute('src')).toMatch(/^blob:/))
    rerender(view(20))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    await flushGridWork()
    for (let frameIndex = 21; frameIndex <= 52; frameIndex += 1) rerender(view(frameIndex))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
    await flushGridWork()

    expect(fetchMock.mock.calls.map(([, init]) => {
      const filenames = JSON.parse(String(init?.body)).filenames as string[]
      return [filenames[0], filenames.at(-1)]
    })).toEqual([
      ['1.png', '32.png'],
      ['33.png', '64.png'],
      ['65.png', '96.png'],
    ])
    const requestsAfterThirdBatch = fetchMock.mock.calls.length
    rerender(view(53))
    expect(screen.queryByText('正在同步占据图')).not.toBeInTheDocument()
    expect(container.querySelector('.occupancy-stage img')?.getAttribute('src')).toMatch(/^blob:/)
    await waitFor(() => expect(screen.getByText('10:00:54.000')).toBeInTheDocument())
    expect(fetchMock).toHaveBeenCalledTimes(requestsAfterThirdBatch)
  })

  it('protects reverse-prefetched frames while a third batch trims the cache', async () => {
    let clock = 0
    vi.spyOn(Date, 'now').mockImplementation(() => ++clock)
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      const filenames = JSON.parse(String(init?.body)).filenames as string[]
      return Promise.resolve(batchResponse(filenames))
    })
    vi.stubGlobal('fetch', fetchMock)
    const frames = Array.from({ length: 96 }, (_, index) => makeFrame(index + 1))
    const view = (frameIndex: number) => (
      <OccupancyPanel datasetId="example" frame={frames[frameIndex]} frameIndex={frameIndex} allFrames={frames} />
    )
    const { container, rerender } = render(view(95))

    await waitFor(() => expect(container.querySelector('.occupancy-stage img')?.getAttribute('src')).toMatch(/^blob:/))
    rerender(view(94))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    await flushGridWork()
    for (let frameIndex = 93; frameIndex >= 74; frameIndex -= 1) rerender(view(frameIndex))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
    await flushGridWork()
    for (let frameIndex = 73; frameIndex >= 42; frameIndex -= 1) rerender(view(frameIndex))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4))
    await flushGridWork()

    expect(fetchMock.mock.calls.map(([, init]) => {
      const filenames = JSON.parse(String(init?.body)).filenames as string[]
      return [filenames[0], filenames.at(-1)]
    })).toEqual([
      ['96.png', '96.png'],
      ['95.png', '64.png'],
      ['63.png', '32.png'],
      ['31.png', '1.png'],
    ])
    const requestsAfterThirdReverseBatch = fetchMock.mock.calls.length
    rerender(view(41))
    expect(screen.queryByText('正在同步占据图')).not.toBeInTheDocument()
    expect(container.querySelector('.occupancy-stage img')?.getAttribute('src')).toMatch(/^blob:/)
    await waitFor(() => expect(screen.getByText('10:00:42.000')).toBeInTheDocument())
    expect(fetchMock).toHaveBeenCalledTimes(requestsAfterThirdReverseBatch)
  })
})
