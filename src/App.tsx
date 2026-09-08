import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { MAP_NAMES } from '../shared/types'
import type {
  DatasetDeleteResult,
  DatasetDetail,
  DatasetMapNameResult,
  DatasetSummary,
  DatasetTrimResult,
  MapName,
  PreviewSessionResult,
} from '../shared/types'
import { CameraWall } from './components/CameraWall'
import { OccupancyPanel } from './components/OccupancyPanel'
import { PlaybackControls } from './components/PlaybackControls'
import { TelemetryPanel } from './components/TelemetryPanel'
import { TrainingDataPage } from './components/TrainingDataPage'
import { TrajectoryCanvas } from './components/TrajectoryCanvas'
import { findNearestFrame } from './lib/playback'

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(typeof body.error === 'string' ? body.error : `请求失败（${response.status}）`)
  }
  return body as T
}

type TrimInputMode = 'hms' | 'seconds'

interface DurationParts {
  hours: string
  minutes: string
  seconds: string
}

const EMPTY_DURATION: DurationParts = { hours: '0', minutes: '0', seconds: '0' }

function durationPartsToSeconds(parts: DurationParts): { valid: boolean; seconds: number } {
  const hours = Number(parts.hours || 0)
  const minutes = Number(parts.minutes || 0)
  const seconds = Number(parts.seconds || 0)
  const valid = Number.isInteger(hours) && hours >= 0
    && Number.isInteger(minutes) && minutes >= 0 && minutes < 60
    && Number.isFinite(seconds) && seconds >= 0 && seconds < 60
  return { valid, seconds: valid ? hours * 3600 + minutes * 60 + seconds : Number.NaN }
}

function secondsInputToValue(value: string): { valid: boolean; seconds: number } {
  const seconds = Number(value)
  const valid = value.trim() !== '' && Number.isFinite(seconds) && seconds >= 0
  return { valid, seconds: valid ? seconds : Number.NaN }
}

function secondsToDurationParts(value: string): DurationParts {
  const total = Number(value || 0)
  if (!Number.isFinite(total) || total < 0) return { ...EMPTY_DURATION }
  const totalMilliseconds = Math.round(total * 1000)
  const hours = Math.floor(totalMilliseconds / 3_600_000)
  const remainingAfterHours = totalMilliseconds - hours * 3_600_000
  const minutes = Math.floor(remainingAfterHours / 60_000)
  const seconds = (remainingAfterHours - minutes * 60_000) / 1000
  return { hours: String(hours), minutes: String(minutes), seconds: String(seconds) }
}

function formatHms(totalSeconds: number): string {
  const totalTenths = Math.round(Math.max(0, totalSeconds) * 10)
  const hours = Math.floor(totalTenths / 36_000)
  const remainingAfterHours = totalTenths - hours * 36_000
  const minutes = Math.floor(remainingAfterHours / 600)
  const seconds = (remainingAfterHours - minutes * 600) / 10
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${seconds.toFixed(1).padStart(4, '0')}`
}

function videoTimelineTime(video: HTMLVideoElement): number {
  const streamStart = video.dataset.preview === 'true' ? Number(video.dataset.streamStart ?? 0) : 0
  return streamStart + video.currentTime
}

function setVideoTimelineTime(video: HTMLVideoElement, target: number): void {
  const streamStart = video.dataset.preview === 'true' ? Number(video.dataset.streamStart ?? 0) : 0
  const localTarget = Math.max(0, target - streamStart)
  if (Math.abs(video.currentTime - localTarget) > 0.015) video.currentTime = localTarget
}

function DatasetPreviewApp({ onOpenTraining }: { onOpenTraining: () => void }) {
  const [datasets, setDatasets] = useState<DatasetSummary[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [detail, setDetail] = useState<DatasetDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [currentTime, setCurrentTime] = useState(0)
  const [isPlaying, setIsPlaying] = useState(false)
  const [mediaErrors, setMediaErrors] = useState<Set<string>>(new Set())
  const [reloadRevision, setReloadRevision] = useState(0)
  const [dialog, setDialog] = useState<'delete' | 'trim' | 'map' | null>(null)
  const [mapNameSelection, setMapNameSelection] = useState<MapName | ''>('')
  const [trimInputMode, setTrimInputMode] = useState<TrimInputMode>('hms')
  const [trimStart, setTrimStart] = useState('0')
  const [trimEnd, setTrimEnd] = useState('0')
  const [trimStartParts, setTrimStartParts] = useState<DurationParts>({ ...EMPTY_DURATION })
  const [trimEndParts, setTrimEndParts] = useState<DurationParts>({ ...EMPTY_DURATION })
  const [mutationPending, setMutationPending] = useState(false)
  const [mutationError, setMutationError] = useState('')
  const [notice, setNotice] = useState('')
  const [previewSession, setPreviewSession] = useState<PreviewSessionResult | null>(null)
  const [previewStartTime, setPreviewStartTime] = useState(0)
  const [previewRevision, setPreviewRevision] = useState(0)
  const [previewStarting, setPreviewStarting] = useState(false)
  const [previewStatus, setPreviewStatus] = useState('原始码率')
  const [bufferingIds, setBufferingIds] = useState<Set<string>>(new Set())
  const [occupancyBuffering, setOccupancyBuffering] = useState(false)
  const videoRefs = useRef(new Map<string, HTMLVideoElement>())
  const frameRequest = useRef<number | null>(null)
  const previewSessionRef = useRef<PreviewSessionResult | null>(null)
  const previewSeekTimer = useRef<number | null>(null)
  const previewRequestRevision = useRef(0)
  const isPlayingRef = useRef(false)
  const bufferingIdsRef = useRef(new Set<string>())
  const occupancyBufferingRef = useRef(false)
  const hardSeekTimes = useRef(new WeakMap<HTMLVideoElement, number>())
  isPlayingRef.current = isPlaying

  const releasePreviewSession = useCallback((updateState = true) => {
    previewRequestRevision.current += 1
    const sessionId = previewSessionRef.current?.sessionId
    previewSessionRef.current = null
    if (updateState) setPreviewSession(null)
    if (sessionId) {
      void fetch(`/api/preview/sessions/${encodeURIComponent(sessionId)}`, {
        method: 'DELETE',
        keepalive: true,
      }).catch(() => undefined)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    fetchJson<DatasetSummary[]>('/api/datasets', { signal: controller.signal })
      .then((items) => {
        setDatasets(items)
        setSelectedId((current) => current || items[0]?.id || '')
      })
      .catch((reason: unknown) => {
        if ((reason as Error).name !== 'AbortError') setError((reason as Error).message)
      })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [])

  useEffect(() => {
    releasePreviewSession()
    if (!selectedId) {
      setDetail(null)
      setLoading(false)
      return
    }
    const controller = new AbortController()
    setLoading(true)
    setError('')
    setIsPlaying(false)
    setCurrentTime(0)
    setDetail(null)
    setMediaErrors(new Set())
    bufferingIdsRef.current.clear()
    setBufferingIds(new Set())
    occupancyBufferingRef.current = false
    setOccupancyBuffering(false)
    setPreviewStatus('原始码率')
    for (const video of videoRefs.current.values()) video.pause()
    videoRefs.current.clear()
    fetchJson<DatasetDetail>(`/api/datasets/${encodeURIComponent(selectedId)}`, { signal: controller.signal })
      .then(setDetail)
      .catch((reason: unknown) => {
        if ((reason as Error).name !== 'AbortError') setError((reason as Error).message)
      })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [releasePreviewSession, reloadRevision, selectedId])

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

  const pauseMedia = useCallback(() => {
    for (const video of getOrderedVideos()) {
      video.pause()
      video.playbackRate = 1
    }
  }, [getOrderedVideos])

  const playGroup = useCallback(async (videos = getOrderedVideos()) => {
    if (!videos.length) return false
    const results = await Promise.allSettled(videos.map((video) => video.play()))
    const startedTogether = results.every((result) => result.status === 'fulfilled')
    if (!startedTogether) {
      for (const video of videos) video.pause()
      setError('部分视频未能开始播放，已暂停全部视角')
    }
    return startedTogether
  }, [getOrderedVideos])

  const handleBufferingChange = useCallback((cameraId: string, buffering: boolean) => {
    const next = new Set(bufferingIdsRef.current)
    if (buffering) next.add(cameraId)
    else next.delete(cameraId)
    bufferingIdsRef.current = next
    setBufferingIds(next)
    if (buffering && isPlayingRef.current) pauseMedia()
  }, [pauseMedia])

  const handleOccupancyBufferingChange = useCallback((buffering: boolean) => {
    if (occupancyBufferingRef.current === buffering) return
    occupancyBufferingRef.current = buffering
    setOccupancyBuffering(buffering)
    if (buffering && isPlayingRef.current) pauseMedia()
  }, [pauseMedia])

  const seek = useCallback(
    (seconds: number) => {
      const duration = detail?.timeline.duration ?? 0
      const target = Math.min(duration, Math.max(0, seconds))
      setCurrentTime(target)
      if (previewSessionRef.current?.enabled) {
        pauseMedia()
        const waiting = new Set(detail?.cameras.filter((camera) => camera.available).map((camera) => camera.id) ?? [])
        bufferingIdsRef.current = waiting
        setBufferingIds(waiting)
        if (previewSeekTimer.current !== null) window.clearTimeout(previewSeekTimer.current)
        previewSeekTimer.current = window.setTimeout(() => {
          setPreviewStartTime(target)
          setPreviewRevision((revision) => revision + 1)
          previewSeekTimer.current = null
        }, 180)
        return
      }
      for (const video of getOrderedVideos()) {
        setVideoTimelineTime(video, target)
      }
    },
    [detail, getOrderedVideos, pauseMedia],
  )

  const pause = useCallback(() => {
    pauseMedia()
    if (previewSeekTimer.current !== null) {
      window.clearTimeout(previewSeekTimer.current)
      previewSeekTimer.current = null
    }
    setIsPlaying(false)
    setPreviewStarting(false)
    releasePreviewSession()
    setPreviewStatus('原始码率')
  }, [pauseMedia, releasePreviewSession])

  const openDialog = useCallback((nextDialog: 'delete' | 'trim' | 'map') => {
    setMutationError('')
    if (nextDialog === 'map') {
      setMapNameSelection(detail?.mapName ?? '')
      setDialog(nextDialog)
      return
    }
    const keepTo = nextDialog === 'trim' ? (detail?.timeline.duration ?? 0) : 0
    setTrimStart('0')
    setTrimEnd(String(keepTo))
    setTrimStartParts({ ...EMPTY_DURATION })
    setTrimEndParts(secondsToDurationParts(String(keepTo)))
    setDialog(nextDialog)
  }, [detail])

  const changeTrimInputMode = useCallback((nextMode: TrimInputMode) => {
    if (nextMode === trimInputMode) return
    if (nextMode === 'hms') {
      setTrimStartParts(secondsToDurationParts(trimStart))
      setTrimEndParts(secondsToDurationParts(trimEnd))
    } else {
      const start = durationPartsToSeconds(trimStartParts)
      const end = durationPartsToSeconds(trimEndParts)
      setTrimStart(start.valid ? String(start.seconds) : '')
      setTrimEnd(end.valid ? String(end.seconds) : '')
    }
    setTrimInputMode(nextMode)
  }, [trimEnd, trimEndParts, trimInputMode, trimStart, trimStartParts])

  const closeDialog = useCallback(() => {
    if (!mutationPending) setDialog(null)
  }, [mutationPending])

  const handleDelete = useCallback(async () => {
    if (!detail || mutationPending) return
    pause()
    setMutationPending(true)
    setMutationError('')
    setNotice('')
    try {
      await fetchJson<DatasetDeleteResult>(`/api/datasets/${encodeURIComponent(detail.id)}`, { method: 'DELETE' })
      const remaining = await fetchJson<DatasetSummary[]>('/api/datasets')
      setDatasets(remaining)
      setDialog(null)
      setDetail(null)
      setSelectedId(remaining[0]?.id ?? '')
      setNotice(`已删除数据集 ${detail.title || detail.id} 的 Meta 和视频`)
    } catch (reason) {
      setMutationError((reason as Error).message)
    } finally {
      setMutationPending(false)
    }
  }, [detail, mutationPending, pause])

  const handleTrim = useCallback(async () => {
    if (!detail || mutationPending) return
    const startPartsValue = durationPartsToSeconds(trimStartParts)
    const endPartsValue = durationPartsToSeconds(trimEndParts)
    const startSecondsValue = secondsInputToValue(trimStart)
    const endSecondsValue = secondsInputToValue(trimEnd)
    const keepFromSeconds = trimInputMode === 'hms' ? startPartsValue.seconds : startSecondsValue.seconds
    const keepToSeconds = trimInputMode === 'hms' ? endPartsValue.seconds : endSecondsValue.seconds
    const trimStartSeconds = Math.max(0, keepFromSeconds)
    const trimEndSeconds = Math.max(0, detail.timeline.duration - keepToSeconds)
    pause()
    setMutationPending(true)
    setMutationError('')
    setNotice('')
    try {
      const result = await fetchJson<DatasetTrimResult>(`/api/datasets/${encodeURIComponent(detail.id)}/trim`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ trimStart: trimStartSeconds, trimEnd: trimEndSeconds }),
      })
      const items = await fetchJson<DatasetSummary[]>('/api/datasets')
      setDatasets(items)
      setDialog(null)
      setDetail(null)
      setReloadRevision((revision) => revision + 1)
      const warning = result.warnings.length ? `；${result.warnings.join('；')}` : ''
      setNotice(
        `裁剪完成：保留 ${result.duration.toFixed(1)} 秒，删除 ${result.removedFrames} 帧 Meta 和 ${result.removedGrids} 张占据图${warning}`,
      )
    } catch (reason) {
      setMutationError((reason as Error).message)
    } finally {
      setMutationPending(false)
    }
  }, [detail, mutationPending, pause, trimEnd, trimEndParts, trimInputMode, trimStart, trimStartParts])

  const handleMapName = useCallback(async () => {
    if (!detail || !mapNameSelection || mutationPending) return
    setMutationPending(true)
    setMutationError('')
    setNotice('')
    try {
      const result = await fetchJson<DatasetMapNameResult>(
        `/api/datasets/${encodeURIComponent(detail.id)}/map-name`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mapName: mapNameSelection }),
        },
      )
      setDetail((current) => current ? { ...current, mapName: result.mapName } : current)
      setDialog(null)
      setNotice(`地图标签已写入 ${result.labeledFrames.toLocaleString()} 帧：${result.mapName}`)
    } catch (reason) {
      setMutationError((reason as Error).message)
    } finally {
      setMutationPending(false)
    }
  }, [detail, mapNameSelection, mutationPending])

  const togglePlayback = useCallback(async () => {
    const videos = getOrderedVideos()
    if (!videos.length) return
    if (isPlaying || previewStarting) {
      pause()
      return
    }
    if (detail && currentTime >= detail.timeline.duration - 0.05) seek(0)
    const target = currentTime >= (detail?.timeline.duration ?? 0) - 0.05 ? 0 : currentTime
    setError('')
    setPreviewStarting(true)
    pauseMedia()
    const requestRevision = ++previewRequestRevision.current
    try {
      const session = await fetchJson<PreviewSessionResult>('/api/preview/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ streams: videos.length }),
      })
      if (requestRevision !== previewRequestRevision.current) {
        if (session.sessionId) {
          void fetch(`/api/preview/sessions/${encodeURIComponent(session.sessionId)}`, {
            method: 'DELETE',
            keepalive: true,
          }).catch(() => undefined)
        }
        return
      }
      if (session.enabled && session.sessionId) {
        previewSessionRef.current = session
        setPreviewSession(session)
        setPreviewStartTime(target)
        setPreviewRevision((revision) => revision + 1)
        const waiting = new Set(detail?.cameras.filter((camera) => camera.available).map((camera) => camera.id) ?? [])
        bufferingIdsRef.current = waiting
        setBufferingIds(waiting)
        setPreviewStatus(`GPU ${session.gpuIndex} · ${session.profile?.width ?? 480}px/${session.profile?.fps ?? 10}fps`)
        setIsPlaying(true)
        return
      }
      setPreviewStatus(session.reason || '原始码率')
    } catch {
      if (requestRevision === previewRequestRevision.current) setPreviewStatus('GPU 检测失败 · 原始码率')
    } finally {
      if (requestRevision === previewRequestRevision.current) setPreviewStarting(false)
    }

    if (requestRevision !== previewRequestRevision.current) return

    for (const video of videos) setVideoTimelineTime(video, target)
    setIsPlaying(true)
    if (!occupancyBufferingRef.current && !await playGroup(videos)) setIsPlaying(false)
  }, [currentTime, detail, getOrderedVideos, isPlaying, pause, pauseMedia, playGroup, previewStarting, seek])

  useEffect(() => {
    if (!isPlaying || bufferingIds.size || occupancyBuffering) return
    const videos = getOrderedVideos()
    if (!videos.length) return
    void playGroup(videos).then((started) => {
      if (!started) setIsPlaying(false)
    })
  }, [bufferingIds, getOrderedVideos, isPlaying, occupancyBuffering, playGroup, previewRevision])

  useEffect(() => {
    if (!isPlaying) return
    const update = () => {
      const videos = getOrderedVideos()
      const master = videos[0]
      if (!master) {
        setIsPlaying(false)
        return
      }
      const masterTime = videoTimelineTime(master)
      setCurrentTime(masterTime)
      for (const video of videos.slice(1)) {
        if (video.readyState < HTMLMediaElement.HAVE_METADATA) continue
        const difference = masterTime - videoTimelineTime(video)
        const absoluteDifference = Math.abs(difference)
        if (absoluteDifference < 0.04) video.playbackRate = 1
        else if (absoluteDifference <= 0.5) video.playbackRate = difference > 0 ? 1.04 : 0.96
        else {
          const now = performance.now()
          const lastHardSeek = hardSeekTimes.current.get(video) ?? -Infinity
          if (now - lastHardSeek >= 1000) {
            setVideoTimelineTime(video, masterTime)
            hardSeekTimes.current.set(video, now)
          }
        }
      }
      if (master.ended || masterTime >= (detail?.timeline.duration ?? Infinity) - 0.01) {
        pauseMedia()
        setCurrentTime(detail?.timeline.duration ?? masterTime)
        setIsPlaying(false)
        releasePreviewSession()
        setPreviewStatus('原始码率')
        return
      }
      frameRequest.current = window.requestAnimationFrame(update)
    }
    frameRequest.current = window.requestAnimationFrame(update)
    return () => {
      if (frameRequest.current !== null) window.cancelAnimationFrame(frameRequest.current)
    }
  }, [detail, getOrderedVideos, isPlaying, pauseMedia, releasePreviewSession])

  const handlePreviewFailure = useCallback(() => {
    pauseMedia()
    releasePreviewSession()
    setPreviewStatus('GPU 预览中断 · 已回退原始码率')
    const waiting = new Set(detail?.cameras.filter((camera) => camera.available).map((camera) => camera.id) ?? [])
    bufferingIdsRef.current = waiting
    setBufferingIds(waiting)
  }, [detail, pauseMedia, releasePreviewSession])

  useEffect(
    () => () => {
      for (const video of videoRefs.current.values()) video.pause()
      if (previewSeekTimer.current !== null) window.clearTimeout(previewSeekTimer.current)
      releasePreviewSession(false)
    },
    [releasePreviewSession],
  )

  const timestamp = (detail?.timeline.from ?? 0) + currentTime
  const gridFrames = useMemo(
    () => detail?.frames.filter((frame) => frame.grid.valid && frame.grid.filename) ?? [],
    [detail],
  )
  const active = useMemo(
    () => (detail ? findNearestFrame(detail.frames, timestamp) : null),
    [detail, timestamp],
  )
  const activeGrid = useMemo(
    () => findNearestFrame(gridFrames, timestamp),
    [gridFrames, timestamp],
  )
  const selected = datasets.find((dataset) => dataset.id === selectedId)
  const startPartsValue = durationPartsToSeconds(trimStartParts)
  const endPartsValue = durationPartsToSeconds(trimEndParts)
  const startSecondsValue = secondsInputToValue(trimStart)
  const endSecondsValue = secondsInputToValue(trimEnd)
  const keepFromSeconds = trimInputMode === 'hms' ? startPartsValue.seconds : startSecondsValue.seconds
  const keepToSeconds = trimInputMode === 'hms' ? endPartsValue.seconds : endSecondsValue.seconds
  const trimValuesValid = trimInputMode === 'hms'
    ? startPartsValue.valid && endPartsValue.valid
    : startSecondsValue.valid && endSecondsValue.valid
  const currentDuration = detail?.timeline.duration ?? 0
  const trimRemaining = keepToSeconds - keepFromSeconds
  const trimRangeValid = trimValuesValid
    && keepFromSeconds >= 0
    && keepToSeconds <= currentDuration + 0.0005
    && trimRemaining >= 0.1
  const removesData = keepFromSeconds > 0.0005 || keepToSeconds < currentDuration - 0.0005
  const trimCanSubmit = trimRangeValid && removesData
  const trimRangeMessage = !trimValuesValid
    ? '请输入有效时间'
    : keepToSeconds > currentDuration + 0.0005
      ? '保留终点不能超过当前长度'
      : keepToSeconds <= keepFromSeconds
        ? '保留终点必须晚于起点'
        : trimRemaining < 0.1
          ? '至少需要保留 0.1 秒'
          : !removesData
            ? '请调整起点或终点'
            : ''
  const trimDurationLabel = trimInputMode === 'hms'
    ? formatHms(currentDuration)
    : `${currentDuration.toFixed(1)} 秒`
  const trimRemainingLabel = trimInputMode === 'hms'
    ? formatHms(trimRemaining)
    : `${trimRemaining.toFixed(1)} 秒`
  const trimmedFromStart = trimRangeValid ? keepFromSeconds : 0
  const trimmedFromEnd = trimRangeValid ? Math.max(0, currentDuration - keepToSeconds) : 0

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

  if (!detail && datasets.length === 0) {
    return (
      <main className="state-screen">
        <span className="state-symbol state-symbol--empty">0</span>
        <h1>暂无可预览的数据集</h1>
        <p>tmp_data 和 visual_nav_mv 中没有找到成对的 Meta 和视频目录。</p>
        {notice ? <p className="state-notice">{notice}</p> : null}
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
        <nav className="workspace-tabs" aria-label="数据视图">
          <button type="button" className="is-active" aria-current="page">采集预览</button>
          <button type="button" onClick={onOpenTraining}>训练数据</button>
        </nav>
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
          <button
            className={`dataset-action${detail.mapName ? ' dataset-action--labeled' : ''}`}
            type="button"
            onClick={() => openDialog('map')}
            aria-label="标注地图名称"
            title={detail.mapName ? `地图标签：${detail.mapName}` : '标注地图名称'}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M4 5v6.2L12.8 20 20 12.8 11.2 4H5a1 1 0 0 0-1 1Z" />
              <circle cx="8" cy="8" r="1.3" />
            </svg>
          </button>
          <button className="dataset-action" type="button" onClick={() => openDialog('trim')} aria-label="裁剪数据集片段" title="裁剪开头或结尾">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="m4 6 16 12M4 18 20 6" />
              <circle cx="4" cy="6" r="2.2" />
              <circle cx="4" cy="18" r="2.2" />
            </svg>
          </button>
          <button className="dataset-action dataset-action--danger" type="button" onClick={() => openDialog('delete')} aria-label="删除数据集" title="删除数据集">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M4 7h16M9 7V4h6v3m3 0-1 14H7L6 7m4 4v6m4-6v6" />
            </svg>
          </button>
        </div>
      </header>

      <main className="dashboard">
        <CameraWall
          key={detail.id}
          cameras={detail.cameras}
          registerVideo={registerVideo}
          onMediaError={handleMediaError}
          onBufferingChange={handleBufferingChange}
          onPreviewFailure={handlePreviewFailure}
          previewSessionId={previewSession?.sessionId ?? null}
          previewStartTime={previewStartTime}
          previewRevision={previewRevision}
          initialTime={currentTime}
        />
        <aside className="side-column">
          <OccupancyPanel
            key={detail.id}
            datasetId={detail.id}
            frame={activeGrid?.frame ?? null}
            frameIndex={activeGrid?.index ?? -1}
            allFrames={gridFrames}
            onBufferingChange={handleOccupancyBufferingChange}
          />
          <TelemetryPanel frame={active?.frame ?? null} mapName={detail.mapName} />
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
        <i />
        <span>地图 {detail.mapName ?? '未标注'}</span>
        <i />
        <span>{previewStarting ? '正在检测 GPU' : previewStatus}</span>
        {notice ? <span className="playback-notice">{notice}</span> : null}
        {error ? <span className="playback-error">{error}</span> : null}
      </div>
      <PlaybackControls
        currentTime={currentTime}
        duration={detail.timeline.duration}
        isPlaying={isPlaying}
        isBuffering={previewStarting || (isPlaying && bufferingIds.size > 0)}
        onToggle={togglePlayback}
        onSeek={seek}
      />
      {dialog ? (
        <div className="dialog-backdrop" onMouseDown={(event) => {
          if (event.target === event.currentTarget) closeDialog()
        }}>
          <section className="dataset-dialog" role="dialog" aria-modal="true" aria-labelledby="dataset-dialog-title">
            <div className="dialog-heading">
              <div>
                <span className="eyebrow">DATA MANAGEMENT</span>
                <h2 id="dataset-dialog-title">
                  {dialog === 'delete' ? '删除数据集' : dialog === 'map' ? '标注地图名称' : '裁剪首尾片段'}
                </h2>
              </div>
              <button className="dialog-close" type="button" onClick={closeDialog} disabled={mutationPending} aria-label="关闭">×</button>
            </div>
            {dialog === 'delete' ? (
              <div className="dialog-body">
                <p>确定删除“{detail.title || detail.id}”吗？</p>
                <div className="danger-callout">
                  此操作会永久删除该数据集的全部 Meta（含占据图）和视频文件，无法撤销。
                </div>
                {mutationError ? <p className="dialog-error" role="alert">{mutationError}</p> : null}
                <div className="dialog-actions">
                  <button type="button" className="secondary-button" onClick={closeDialog} disabled={mutationPending}>取消</button>
                  <button type="button" className="danger-button" onClick={handleDelete} disabled={mutationPending}>
                    {mutationPending ? '正在删除…' : '确认永久删除'}
                  </button>
                </div>
              </div>
            ) : dialog === 'map' ? (
              <form className="dialog-body" onSubmit={(event) => {
                event.preventDefault()
                if (mapNameSelection) void handleMapName()
              }}>
                <p>为“{detail.title || detail.id}”选择小车所在的地图。</p>
                <label className="map-name-field">
                  <span>地图名称</span>
                  <select
                    aria-label="地图名称"
                    value={mapNameSelection}
                    onChange={(event) => setMapNameSelection(event.target.value as MapName | '')}
                    disabled={mutationPending}
                  >
                    <option value="">请选择地图标签</option>
                    {MAP_NAMES.map((mapName) => <option key={mapName} value={mapName}>{mapName}</option>)}
                  </select>
                </label>
                <div className="info-callout">
                  确认后会在当前 Meta 目录的 frames.jsonl 每一行写入 map_name；已有标签会被本次选择覆盖。
                </div>
                {mutationError ? <p className="dialog-error" role="alert">{mutationError}</p> : null}
                <div className="dialog-actions">
                  <button type="button" className="secondary-button" onClick={closeDialog} disabled={mutationPending}>取消</button>
                  <button type="submit" className="primary-button" disabled={mutationPending || !mapNameSelection}>
                    {mutationPending ? '正在写入标签…' : '确认标签'}
                  </button>
                </div>
              </form>
            ) : (
              <form className="dialog-body" onSubmit={(event) => {
                event.preventDefault()
                if (trimCanSubmit) void handleTrim()
              }}>
                <p>选择希望保留的起点和终点，视频、Meta 帧和对应占据图会按同一时间区间同步裁剪。</p>
                <div className="trim-format-row">
                  <span>时间格式</span>
                  <div className="trim-format-switch" role="group" aria-label="时间输入格式">
                    <button type="button" aria-pressed={trimInputMode === 'hms'} onClick={() => changeTrimInputMode('hms')} disabled={mutationPending}>时分秒</button>
                    <button type="button" aria-pressed={trimInputMode === 'seconds'} onClick={() => changeTrimInputMode('seconds')} disabled={mutationPending}>纯秒</button>
                  </div>
                </div>
                <div className="trim-inputs">
                  {trimInputMode === 'seconds' ? (
                    <>
                      <label>
                        <span>保留起点</span>
                        <span className="number-input"><input aria-label="保留起点秒数" type="number" min="0" step="0.1" value={trimStart} onChange={(event) => setTrimStart(event.target.value)} disabled={mutationPending} /><i>秒</i></span>
                      </label>
                      <label>
                        <span>保留终点</span>
                        <span className="number-input"><input aria-label="保留终点秒数" type="number" min="0" step="0.1" value={trimEnd} onChange={(event) => setTrimEnd(event.target.value)} disabled={mutationPending} /><i>秒</i></span>
                      </label>
                    </>
                  ) : (
                    <>
                      <fieldset className="hms-input-group">
                        <legend>保留起点</legend>
                        <div>
                          <label className="time-part"><input aria-label="保留起点小时" type="number" min="0" step="1" value={trimStartParts.hours} onChange={(event) => setTrimStartParts((parts) => ({ ...parts, hours: event.target.value }))} disabled={mutationPending} /><i>时</i></label>
                          <label className="time-part"><input aria-label="保留起点分钟" type="number" min="0" max="59" step="1" value={trimStartParts.minutes} onChange={(event) => setTrimStartParts((parts) => ({ ...parts, minutes: event.target.value }))} disabled={mutationPending} /><i>分</i></label>
                          <label className="time-part"><input aria-label="保留起点秒" type="number" min="0" max="59.999" step="0.1" value={trimStartParts.seconds} onChange={(event) => setTrimStartParts((parts) => ({ ...parts, seconds: event.target.value }))} disabled={mutationPending} /><i>秒</i></label>
                        </div>
                      </fieldset>
                      <fieldset className="hms-input-group">
                        <legend>保留终点</legend>
                        <div>
                          <label className="time-part"><input aria-label="保留终点小时" type="number" min="0" step="1" value={trimEndParts.hours} onChange={(event) => setTrimEndParts((parts) => ({ ...parts, hours: event.target.value }))} disabled={mutationPending} /><i>时</i></label>
                          <label className="time-part"><input aria-label="保留终点分钟" type="number" min="0" max="59" step="1" value={trimEndParts.minutes} onChange={(event) => setTrimEndParts((parts) => ({ ...parts, minutes: event.target.value }))} disabled={mutationPending} /><i>分</i></label>
                          <label className="time-part"><input aria-label="保留终点秒" type="number" min="0" max="59.999" step="0.1" value={trimEndParts.seconds} onChange={(event) => setTrimEndParts((parts) => ({ ...parts, seconds: event.target.value }))} disabled={mutationPending} /><i>秒</i></label>
                        </div>
                      </fieldset>
                    </>
                  )}
                </div>
                <div className={`trim-preview${trimCanSubmit ? '' : ' trim-preview--invalid'}`}>
                  <span>当前长度 {trimDurationLabel}</span>
                  <div>
                    <strong>{trimCanSubmit ? `将保留 ${trimRemainingLabel}` : trimRangeMessage}</strong>
                    {trimCanSubmit ? <small>删除开头 {trimmedFromStart.toFixed(1)} 秒 · 结尾 {trimmedFromEnd.toFixed(1)} 秒</small> : null}
                  </div>
                </div>
                <div className="danger-callout">裁剪会重新编码视频，并永久移除保留区间以外的数据；处理期间请勿关闭服务。</div>
                {mutationError ? <p className="dialog-error" role="alert">{mutationError}</p> : null}
                <div className="dialog-actions">
                  <button type="button" className="secondary-button" onClick={closeDialog} disabled={mutationPending}>取消</button>
                  <button type="submit" className="danger-button" disabled={mutationPending || !trimCanSubmit}>
                    {mutationPending ? '正在同步裁剪…' : '确认裁剪'}
                  </button>
                </div>
              </form>
            )}
          </section>
        </div>
      ) : null}
    </div>
  )
}

export default function App() {
  const [view, setView] = useState<'source' | 'training'>(
    () => window.location.hash === '#training' ? 'training' : 'source',
  )

  useEffect(() => {
    const handleHashChange = () => setView(window.location.hash === '#training' ? 'training' : 'source')
    window.addEventListener('hashchange', handleHashChange)
    return () => window.removeEventListener('hashchange', handleHashChange)
  }, [])

  const openTraining = useCallback(() => {
    window.location.hash = 'training'
    setView('training')
  }, [])
  const openSource = useCallback(() => {
    window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`)
    setView('source')
  }, [])

  return view === 'training'
    ? <TrainingDataPage onOpenSource={openSource} />
    : <DatasetPreviewApp onOpenTraining={openTraining} />
}
