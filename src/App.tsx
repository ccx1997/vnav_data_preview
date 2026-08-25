import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { DatasetDeleteResult, DatasetDetail, DatasetSummary, DatasetTrimResult } from '../shared/types'
import { CameraWall } from './components/CameraWall'
import { OccupancyPanel } from './components/OccupancyPanel'
import { PlaybackControls } from './components/PlaybackControls'
import { TelemetryPanel } from './components/TelemetryPanel'
import { TrajectoryCanvas } from './components/TrajectoryCanvas'
import { findFreshFrame } from './lib/playback'

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

export default function App() {
  const [datasets, setDatasets] = useState<DatasetSummary[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [detail, setDetail] = useState<DatasetDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [currentTime, setCurrentTime] = useState(0)
  const [isPlaying, setIsPlaying] = useState(false)
  const [mediaErrors, setMediaErrors] = useState<Set<string>>(new Set())
  const [reloadRevision, setReloadRevision] = useState(0)
  const [dialog, setDialog] = useState<'delete' | 'trim' | null>(null)
  const [trimInputMode, setTrimInputMode] = useState<TrimInputMode>('hms')
  const [trimStart, setTrimStart] = useState('0')
  const [trimEnd, setTrimEnd] = useState('0')
  const [trimStartParts, setTrimStartParts] = useState<DurationParts>({ ...EMPTY_DURATION })
  const [trimEndParts, setTrimEndParts] = useState<DurationParts>({ ...EMPTY_DURATION })
  const [mutationPending, setMutationPending] = useState(false)
  const [mutationError, setMutationError] = useState('')
  const [notice, setNotice] = useState('')
  const videoRefs = useRef(new Map<string, HTMLVideoElement>())
  const frameRequest = useRef<number | null>(null)

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
    for (const video of videoRefs.current.values()) video.pause()
    videoRefs.current.clear()
    fetchJson<DatasetDetail>(`/api/datasets/${encodeURIComponent(selectedId)}`, { signal: controller.signal })
      .then(setDetail)
      .catch((reason: unknown) => {
        if ((reason as Error).name !== 'AbortError') setError((reason as Error).message)
      })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [reloadRevision, selectedId])

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

  const openDialog = useCallback((nextDialog: 'delete' | 'trim') => {
    const keepTo = nextDialog === 'trim' ? (detail?.timeline.duration ?? 0) : 0
    setMutationError('')
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
        {notice ? <span className="playback-notice">{notice}</span> : null}
        {error ? <span className="playback-error">{error}</span> : null}
      </div>
      <PlaybackControls
        currentTime={currentTime}
        duration={detail.timeline.duration}
        isPlaying={isPlaying}
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
                <h2 id="dataset-dialog-title">{dialog === 'delete' ? '删除数据集' : '裁剪首尾片段'}</h2>
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
