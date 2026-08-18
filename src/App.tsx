import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { DatasetDetail, DatasetSummary } from '../shared/types'
import { CameraWall } from './components/CameraWall'
import { OccupancyPanel } from './components/OccupancyPanel'
import { PlaybackControls } from './components/PlaybackControls'
import { TelemetryPanel } from './components/TelemetryPanel'
import { TrajectoryCanvas } from './components/TrajectoryCanvas'
import { findFreshFrame } from './lib/playback'

async function fetchJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(typeof body.error === 'string' ? body.error : `请求失败（${response.status}）`)
  }
  return body as T
}

export default function App() {
  const [datasets, setDatasets] = useState<DatasetSummary[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [detail, setDetail] = useState<DatasetDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [currentTime, setCurrentTime] = useState(0)
  const [isPlaying, setIsPlaying] = useState(false)
  const [mediaErrors, setMediaErrors] = useState<Set<string>>(new Set())
  const videoRefs = useRef(new Map<string, HTMLVideoElement>())
  const frameRequest = useRef<number | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    fetchJson<DatasetSummary[]>('/api/datasets', controller.signal)
      .then((items) => {
        setDatasets(items)
        setSelectedId((current) => current || items[0]?.id || '')
        if (items.length === 0) setError('tmp_data 中没有找到可用的数据集')
      })
      .catch((reason: unknown) => {
        if ((reason as Error).name !== 'AbortError') setError((reason as Error).message)
      })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [])

  useEffect(() => {
    if (!selectedId) return
    const controller = new AbortController()
    setLoading(true)
    setError('')
    setIsPlaying(false)
    setCurrentTime(0)
    setDetail(null)
    setMediaErrors(new Set())
    for (const video of videoRefs.current.values()) video.pause()
    videoRefs.current.clear()
    fetchJson<DatasetDetail>(`/api/datasets/${encodeURIComponent(selectedId)}`, controller.signal)
      .then(setDetail)
      .catch((reason: unknown) => {
        if ((reason as Error).name !== 'AbortError') setError((reason as Error).message)
      })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [selectedId])

  const registerVideo = useCallback((cameraId: string, video: HTMLVideoElement | null) => {
    if (video) videoRefs.current.set(cameraId, video)
    else videoRefs.current.delete(cameraId)
  }, [])

  const handleMediaError = useCallback((cameraId: string, failed: boolean) => {
    setMediaErrors((current) => {
      if (failed === current.has(cameraId)) return current
      const next = new Set(current)
      if (failed) next.add(cameraId)
      else next.delete(cameraId)
      return next
    })
  }, [])

  const getOrderedVideos = useCallback(() => {
    if (!detail) return []
    return detail.cameras
      .filter((camera) => camera.available && !mediaErrors.has(camera.id))
      .map((camera) => videoRefs.current.get(camera.id))
      .filter((video): video is HTMLVideoElement => Boolean(video))
  }, [detail, mediaErrors])

  const seek = useCallback(
    (seconds: number) => {
      const duration = detail?.timeline.duration ?? 0
      const target = Math.min(duration, Math.max(0, seconds))
      setCurrentTime(target)
      for (const video of getOrderedVideos()) {
        if (Math.abs(video.currentTime - target) > 0.015) video.currentTime = target
      }
    },
    [detail, getOrderedVideos],
  )

  const pause = useCallback(() => {
    for (const video of getOrderedVideos()) video.pause()
    setIsPlaying(false)
  }, [getOrderedVideos])

  const togglePlayback = useCallback(async () => {
    const videos = getOrderedVideos()
    if (!videos.length) return
    if (isPlaying) {
      pause()
      return
    }
    if (detail && currentTime >= detail.timeline.duration - 0.05) seek(0)
    const target = currentTime >= (detail?.timeline.duration ?? 0) - 0.05 ? 0 : currentTime
    for (const video of videos) video.currentTime = target
    const results = await Promise.allSettled(videos.map((video) => video.play()))
    if (results.some((result) => result.status === 'fulfilled')) setIsPlaying(true)
    else setError('浏览器未能开始播放视频')
  }, [currentTime, detail, getOrderedVideos, isPlaying, pause, seek])

  useEffect(() => {
    if (!isPlaying) return
    const update = () => {
      const videos = getOrderedVideos()
      const master = videos[0]
      if (!master) {
        setIsPlaying(false)
        return
      }
      const masterTime = master.currentTime
      setCurrentTime(masterTime)
      for (const video of videos.slice(1)) {
        if (video.readyState >= HTMLMediaElement.HAVE_METADATA && Math.abs(video.currentTime - masterTime) > 0.1) {
          video.currentTime = masterTime
        }
      }
      if (master.ended || masterTime >= (detail?.timeline.duration ?? Infinity) - 0.01) {
        for (const video of videos) video.pause()
        setCurrentTime(detail?.timeline.duration ?? masterTime)
        setIsPlaying(false)
        return
      }
      frameRequest.current = window.requestAnimationFrame(update)
    }
    frameRequest.current = window.requestAnimationFrame(update)
    return () => {
      if (frameRequest.current !== null) window.cancelAnimationFrame(frameRequest.current)
    }
  }, [detail, getOrderedVideos, isPlaying])

  useEffect(
    () => () => {
      for (const video of videoRefs.current.values()) video.pause()
    },
    [],
  )

  const timestamp = (detail?.timeline.from ?? 0) + currentTime
  const active = useMemo(
    () => (detail ? findFreshFrame(detail.frames, timestamp, 1) : null),
    [detail, timestamp],
  )
  const selected = datasets.find((dataset) => dataset.id === selectedId)

  if (loading && !detail) {
    return (
      <main className="state-screen">
        <div className="loader" />
        <h1>正在读取采集数据</h1>
        <p>扫描视频、位姿与占据图索引…</p>
      </main>
    )
  }

  if (error && !detail) {
    return (
      <main className="state-screen state-screen--error">
        <span className="state-symbol">!</span>
        <h1>无法打开数据预览</h1>
        <p>{error}</p>
      </main>
    )
  }

  if (!detail) return null

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark"><i /></span>
          <div>
            <span>VNAV</span>
            <strong>数据预览</strong>
          </div>
        </div>
        <div className="task-summary">
          <div>
            <span className="eyebrow">CURRENT SESSION</span>
            <strong>{detail.title || detail.id}</strong>
          </div>
          <span className="summary-divider" />
          <div className="summary-item">
            <span>机器人</span>
            <strong>{detail.robotId || '—'}</strong>
          </div>
          <div className="summary-item summary-item--wide">
            <span>采集时间</span>
            <strong>{detail.startedAtLocal || '—'}</strong>
          </div>
        </div>
        <div className="topbar-actions">
          <span className={`integrity-chip${detail.complete ? '' : ' integrity-chip--warning'}`}>
            <span className="status-dot" />
            {detail.complete ? '数据完整' : `${detail.warningCount} 项提示`}
          </span>
          <label className="dataset-select">
            <span>数据集</span>
            <select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>
              {datasets.map((dataset) => (
                <option key={dataset.id} value={dataset.id}>
                  {dataset.title || dataset.id}
                </option>
              ))}
            </select>
          </label>
        </div>
      </header>

      <main className="dashboard">
        <CameraWall
          key={detail.id}
          cameras={detail.cameras}
          registerVideo={registerVideo}
          onMediaError={handleMediaError}
        />
        <aside className="side-column">
          <OccupancyPanel
            datasetId={detail.id}
            frame={active?.frame ?? null}
            frameIndex={active?.index ?? -1}
            allFrames={detail.frames}
          />
          <TelemetryPanel frame={active?.frame ?? null} />
          <TrajectoryCanvas
            frames={detail.frames}
            currentTimestamp={timestamp}
            currentFrame={active?.frame ?? null}
          />
        </aside>
      </main>

      <div className="footer-info">
        <span>{detail.cameras.filter((camera) => camera.available).length} 路视频</span>
        <i />
        <span>{detail.frameCount.toLocaleString()} 帧数据</span>
        <i />
        <span>{selected?.taskId || detail.taskId}</span>
        {error ? <span className="playback-error">{error}</span> : null}
      </div>
      <PlaybackControls
        currentTime={currentTime}
        duration={detail.timeline.duration}
        isPlaying={isPlaying}
        onToggle={togglePlayback}
        onSeek={seek}
      />
    </div>
  )
}
