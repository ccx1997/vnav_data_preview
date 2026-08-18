import { useEffect, useState } from 'react'
import type { FrameSample } from '../../shared/types'

interface OccupancyPanelProps {
  datasetId: string
  frame: FrameSample | null
  frameIndex: number
  allFrames: FrameSample[]
}

export function OccupancyPanel({ datasetId, frame, frameIndex, allFrames }: OccupancyPanelProps) {
  const [imageFailed, setImageFailed] = useState(false)
  const filename = frame?.grid.valid ? frame.grid.filename : null
  const imageUrl = filename
    ? `/media/${encodeURIComponent(datasetId)}/grids/${encodeURIComponent(filename)}`
    : null

  useEffect(() => setImageFailed(false), [imageUrl])

  useEffect(() => {
    if (frameIndex < 0) return
    for (const nextFrame of allFrames.slice(frameIndex + 1, frameIndex + 4)) {
      if (!nextFrame.grid.valid || !nextFrame.grid.filename) continue
      const image = new Image()
      image.src = `/media/${encodeURIComponent(datasetId)}/grids/${encodeURIComponent(nextFrame.grid.filename)}`
    }
  }, [allFrames, datasetId, frameIndex])

  const meters = frame?.grid.width && frame.grid.resolution ? frame.grid.width * frame.grid.resolution : null

  return (
    <section className="panel occupancy-panel" aria-label="占据图">
      <div className="panel-heading panel-heading--compact">
        <div>
          <span className="eyebrow">OCCUPANCY GRID</span>
          <h2>占据图</h2>
        </div>
        <span className="panel-meta">
          {frame?.grid.width && frame.grid.height ? `${frame.grid.width} × ${frame.grid.height}` : '—'}
        </span>
      </div>
      <div className="occupancy-stage">
        {imageUrl && !imageFailed ? (
          <img src={imageUrl} alt="当前时刻占据图" onError={() => setImageFailed(true)} />
        ) : (
          <div className="media-placeholder">
            <span className="status-dot" />
            无对应占据图
          </div>
        )}
        {imageUrl && !imageFailed ? (
          <div className="occupancy-crosshair" aria-hidden="true">
            <span />
          </div>
        ) : null}
        {meters !== null && imageUrl && !imageFailed ? (
          <span className="occupancy-scale">{meters.toFixed(0)} m</span>
        ) : null}
      </div>
      <div className="occupancy-footer">
        <span>{frame?.dateTimeLocal ? frame.dateTimeLocal.split(' ').at(-1) : '等待数据'}</span>
        <span>{frame?.grid.frameId || 'body frame'}</span>
      </div>
    </section>
  )
}
