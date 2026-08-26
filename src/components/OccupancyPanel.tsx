import { useCallback, useEffect, useRef, useState } from 'react'
import { decodeGridBatch } from '../../shared/gridBatch'
import type { FrameSample } from '../../shared/types'

const GRID_BATCH_FRAMES = 32
const PREFETCH_LOW_WATER_FRAMES = 12
const MAX_CACHED_GRIDS = 64
const MAX_CACHE_BYTES = 64 * 1024 * 1024

interface CachedGrid {
  objectUrl: string
  bytes: number
  lastUsed: number
  image: HTMLImageElement
  ready: boolean
  decoded: Promise<boolean>
}

interface DisplayedGrid {
  filename: string
  objectUrl: string
  frame: FrameSample
  direct: boolean
}

interface ActiveBatch {
  controller: AbortController
  filenames: Set<string>
  promise: Promise<boolean>
}

interface OccupancyPanelProps {
  datasetId: string
  frame: FrameSample | null
  frameIndex: number
  allFrames: FrameSample[]
  onBufferingChange?: (buffering: boolean) => void
}

function gridUrl(datasetId: string, filename: string): string {
  return `/media/${encodeURIComponent(datasetId)}/grids/${encodeURIComponent(filename)}`
}

export function OccupancyPanel({
  datasetId,
  frame,
  frameIndex,
  allFrames,
  onBufferingChange,
}: OccupancyPanelProps) {
  const [imageFailed, setImageFailed] = useState(false)
  const [displayedImage, setDisplayedImage] = useState<DisplayedGrid | null>(null)
  const cacheRef = useRef(new Map<string, CachedGrid>())
  const activeBatchRef = useRef<ActiveBatch | null>(null)
  const protectedFilenamesRef = useRef(new Set<string>())
  const requestClockRef = useRef(0)
  const previousFrameIndexRef = useRef(-1)
  const directionRef = useRef(1)
  const disposedRef = useRef(false)
  const bufferingRef = useRef(false)
  const onBufferingChangeRef = useRef(onBufferingChange)
  onBufferingChangeRef.current = onBufferingChange

  const filename = frame?.grid.valid ? frame.grid.filename : null
  const imageUrl = filename ? gridUrl(datasetId, filename) : null

  const reportBuffering = useCallback((buffering: boolean) => {
    if (bufferingRef.current === buffering) return
    bufferingRef.current = buffering
    onBufferingChangeRef.current?.(buffering)
  }, [])

  const trimCache = useCallback(() => {
    const cache = cacheRef.current
    let totalBytes = [...cache.values()].reduce((sum, entry) => sum + entry.bytes, 0)
    while (cache.size > MAX_CACHED_GRIDS || totalBytes > MAX_CACHE_BYTES) {
      const oldest = [...cache.entries()]
        .filter(([candidate]) => !protectedFilenamesRef.current.has(candidate))
        .sort((left, right) => left[1].lastUsed - right[1].lastUsed)[0]
      if (!oldest) break
      cache.delete(oldest[0])
      totalBytes -= oldest[1].bytes
      if (typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(oldest[1].objectUrl)
    }
  }, [])

  const cacheGrid = useCallback((recordFilename: string, payload: Uint8Array) => {
    if (disposedRef.current || cacheRef.current.has(recordFilename) || typeof URL.createObjectURL !== 'function') return
    const blob = new Blob([new Uint8Array(payload)], { type: 'image/png' })
    const objectUrl = URL.createObjectURL(blob)
    const image = new Image()
    image.decoding = 'async'
    image.src = objectUrl
    const entry: CachedGrid = {
      objectUrl,
      bytes: blob.size,
      lastUsed: Date.now(),
      image,
      ready: false,
      decoded: Promise.resolve(false),
    }
    entry.decoded = (typeof image.decode === 'function'
      ? image.decode().then(() => true).catch(() => false)
      : Promise.resolve(true))
      .then((decoded) => {
        if (decoded) {
          entry.ready = true
          entry.bytes = blob.size + image.naturalWidth * image.naturalHeight * 4
          entry.lastUsed = Date.now()
          trimCache()
        }
        return decoded
      })
    cacheRef.current.set(recordFilename, entry)
    trimCache()
  }, [trimCache])

  const requestWindow = useCallback((startIndex: number, direction: number, urgentFilename?: string) => {
    const selectedFrames: FrameSample[] = []
    for (let offset = 0; offset < GRID_BATCH_FRAMES; offset += 1) {
      const candidate = allFrames[startIndex + offset * direction]
      if (!candidate) break
      if (candidate.grid.valid && candidate.grid.filename) selectedFrames.push(candidate)
    }
    const filenames = [...new Set(selectedFrames
      .map((candidate) => candidate.grid.filename)
      .filter((candidate): candidate is string => Boolean(candidate)))]
      .filter((candidate) => !cacheRef.current.has(candidate))
    if (!filenames.length) return Promise.resolve(true)

    const active = activeBatchRef.current
    if (active) {
      if (!urgentFilename || active.filenames.has(urgentFilename)) return active.promise
      active.controller.abort()
    }

    const controller = new AbortController()
    const pending: ActiveBatch = {
      controller,
      filenames: new Set(filenames),
      promise: Promise.resolve(false),
    }
    pending.promise = fetch(`/media/${encodeURIComponent(datasetId)}/grids/batch`, {
      method: 'POST',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filenames }),
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok || typeof response.arrayBuffer !== 'function') return false
        const records = decodeGridBatch(await response.arrayBuffer())
        const requested = pending.filenames
        for (const record of records) {
          if (requested.has(record.filename)) cacheGrid(record.filename, record.bytes)
        }
        return records.length > 0
      })
      .catch(() => false)
      .finally(() => {
        if (activeBatchRef.current === pending) activeBatchRef.current = null
      })
    activeBatchRef.current = pending
    return pending.promise
  }, [allFrames, cacheGrid, datasetId])

  useEffect(() => {
    setImageFailed(false)
    const requestClock = ++requestClockRef.current
    if (!filename || !imageUrl || !frame || frameIndex < 0) {
      protectedFilenamesRef.current.clear()
      setDisplayedImage(null)
      reportBuffering(false)
      return
    }

    const previousIndex = previousFrameIndexRef.current
    if (previousIndex >= 0 && frameIndex !== previousIndex) directionRef.current = frameIndex < previousIndex ? -1 : 1
    previousFrameIndexRef.current = frameIndex
    const protectedFilenames = new Set<string>()
    for (let offset = 0; offset <= PREFETCH_LOW_WATER_FRAMES; offset += 1) {
      const candidateFilename = allFrames[frameIndex + offset * directionRef.current]?.grid.filename
      if (candidateFilename) protectedFilenames.add(candidateFilename)
    }
    protectedFilenamesRef.current = protectedFilenames

    const publish = (entry: CachedGrid) => {
      if (requestClock !== requestClockRef.current || !entry.ready) return
      entry.lastUsed = Date.now()
      setDisplayedImage({ filename, objectUrl: entry.objectUrl, frame, direct: false })
      reportBuffering(false)
    }

    const cached = cacheRef.current.get(filename)
    if (cached?.ready) {
      publish(cached)
      return
    }

    setDisplayedImage(null)
    reportBuffering(true)
    void (async () => {
      if (cached) {
        if (await cached.decoded) publish(cached)
        return
      }
      const loaded = await requestWindow(frameIndex, directionRef.current, filename)
      if (requestClock !== requestClockRef.current) return
      const entry = cacheRef.current.get(filename)
      if (loaded && entry && await entry.decoded) {
        publish(entry)
        return
      }

      // Compatibility fallback for a server that has not yet been restarted with batch support.
      setDisplayedImage({ filename, objectUrl: imageUrl, frame, direct: true })
    })()
  }, [filename, frame, frameIndex, imageUrl, reportBuffering, requestWindow])

  useEffect(() => {
    if (!filename || displayedImage?.filename !== filename || displayedImage.direct || frameIndex < 0) return
    const direction = directionRef.current
    for (let offset = 1; offset <= PREFETCH_LOW_WATER_FRAMES; offset += 1) {
      const index = frameIndex + offset * direction
      const candidate = allFrames[index]
      const candidateFilename = candidate?.grid.filename
      if (candidateFilename && !cacheRef.current.has(candidateFilename)) {
        void requestWindow(index, direction)
        break
      }
    }
  }, [allFrames, displayedImage, filename, frameIndex, requestWindow])

  useEffect(() => {
    disposedRef.current = false
    return () => {
      disposedRef.current = true
      requestClockRef.current += 1
      activeBatchRef.current?.controller.abort()
      activeBatchRef.current = null
      protectedFilenamesRef.current.clear()
      if (bufferingRef.current) onBufferingChangeRef.current?.(false)
      bufferingRef.current = false
      if (typeof URL.revokeObjectURL === 'function') {
        for (const entry of cacheRef.current.values()) URL.revokeObjectURL(entry.objectUrl)
      }
      cacheRef.current.clear()
    }
  }, [])

  const cachedTarget = filename ? cacheRef.current.get(filename) : null
  const visibleImage = displayedImage?.filename === filename
    ? displayedImage
    : filename && cachedTarget?.ready && frame
      ? { filename, objectUrl: cachedTarget.objectUrl, frame, direct: false }
      : null
  const visibleFrame = visibleImage?.frame ?? null
  const meters = visibleFrame?.grid.width && visibleFrame.grid.resolution
    ? visibleFrame.grid.width * visibleFrame.grid.resolution
    : null

  return (
    <section className="panel occupancy-panel" aria-label="占据图">
      <div className="panel-heading panel-heading--compact">
        <div>
          <span className="eyebrow">OCCUPANCY GRID</span>
          <h2>占据图</h2>
        </div>
        <span className="panel-meta">
          {visibleFrame?.grid.width && visibleFrame.grid.height ? `${visibleFrame.grid.width} × ${visibleFrame.grid.height}` : '—'}
        </span>
      </div>
      <div className="occupancy-stage">
        {imageUrl && visibleImage && !imageFailed ? (
          <img
            src={visibleImage.objectUrl}
            alt="当前时刻占据图"
            onLoad={() => {
              if (visibleImage.direct) reportBuffering(false)
            }}
            onError={() => {
              setImageFailed(true)
              reportBuffering(false)
            }}
          />
        ) : (
          <div className="media-placeholder">
            <span className="status-dot" />
            {imageUrl ? (imageFailed ? '占据图不可用' : '正在同步占据图') : '无对应占据图'}
          </div>
        )}
        {imageUrl && visibleImage && !imageFailed ? (
          <div className="occupancy-crosshair" aria-hidden="true">
            <span />
          </div>
        ) : null}
        {meters !== null && imageUrl && visibleImage && !imageFailed ? (
          <span className="occupancy-scale">{meters.toFixed(0)} m</span>
        ) : null}
      </div>
      <div className="occupancy-footer">
        <span>{visibleFrame?.dateTimeLocal ? visibleFrame.dateTimeLocal.split(' ').at(-1) : '等待数据'}</span>
        <span>{visibleFrame?.grid.frameId || 'body frame'}</span>
      </div>
    </section>
  )
}
