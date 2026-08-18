import { useEffect, useMemo, useRef } from 'react'
import type { FrameSample } from '../../shared/types'
import { computeTrajectoryViewport, getTrajectoryFrames } from '../lib/playback'

interface TrajectoryCanvasProps {
  frames: FrameSample[]
  currentTimestamp: number
  currentFrame: FrameSample | null
}

export function TrajectoryCanvas({ frames, currentTimestamp, currentFrame }: TrajectoryCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const scaleRef = useRef<number | null>(null)
  const trajectory = useMemo(
    () => getTrajectoryFrames(frames, currentTimestamp, 30),
    [currentTimestamp, frames],
  )

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const context = canvas.getContext('2d')
    if (!context) return

    const draw = () => {
      const bounds = canvas.getBoundingClientRect()
      const width = Math.max(1, bounds.width)
      const height = Math.max(1, bounds.height)
      const ratio = window.devicePixelRatio || 1
      const targetWidth = Math.round(width * ratio)
      const targetHeight = Math.round(height * ratio)
      if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
        canvas.width = targetWidth
        canvas.height = targetHeight
      }
      context.setTransform(ratio, 0, 0, ratio, 0, 0)
      context.clearRect(0, 0, width, height)
      context.fillStyle = '#0b1419'
      context.fillRect(0, 0, width, height)

      if (!currentFrame || !currentFrame.pose.valid || currentFrame.pose.x === null || currentFrame.pose.y === null) {
        context.fillStyle = '#6f828c'
        context.font = '500 13px Inter, system-ui, sans-serif'
        context.textAlign = 'center'
        context.fillText('无对应轨迹数据', width / 2, height / 2)
        return
      }

      const targetViewport = computeTrajectoryViewport(trajectory, currentFrame, width / height)
      const targetHalfHeight = targetViewport.halfHeight
      const previousScale = scaleRef.current
      const halfHeight = previousScale === null ? targetHalfHeight : previousScale + (targetHalfHeight - previousScale) * 0.16
      scaleRef.current = halfHeight
      const halfWidth = halfHeight * (width / height)
      const centerX = currentFrame.pose.x
      const centerY = currentFrame.pose.y
      const toCanvas = (x: number, y: number): [number, number] => [
        width / 2 + ((x - centerX) / halfWidth) * (width / 2),
        height / 2 - ((y - centerY) / halfHeight) * (height / 2),
      ]

      context.lineWidth = 1
      context.strokeStyle = '#16262e'
      context.beginPath()
      const gridStep = halfHeight <= 2.5 ? 0.5 : halfHeight <= 6 ? 1 : 2
      const xMin = centerX - halfWidth
      const xMax = centerX + halfWidth
      const yMin = centerY - halfHeight
      const yMax = centerY + halfHeight
      for (let x = Math.ceil(xMin / gridStep) * gridStep; x <= xMax; x += gridStep) {
        const [screenX] = toCanvas(x, centerY)
        context.moveTo(screenX, 0)
        context.lineTo(screenX, height)
      }
      for (let y = Math.ceil(yMin / gridStep) * gridStep; y <= yMax; y += gridStep) {
        const [, screenY] = toCanvas(centerX, y)
        context.moveTo(0, screenY)
        context.lineTo(width, screenY)
      }
      context.stroke()

      context.strokeStyle = '#23404b'
      context.beginPath()
      context.moveTo(0, height / 2)
      context.lineTo(width, height / 2)
      context.moveTo(width / 2, 0)
      context.lineTo(width / 2, height)
      context.stroke()

      if (trajectory.length > 1) {
        const gradient = context.createLinearGradient(0, 0, width, 0)
        gradient.addColorStop(0, '#245869')
        gradient.addColorStop(1, '#68d8ed')
        context.strokeStyle = gradient
        context.lineWidth = 2.5
        context.lineJoin = 'round'
        context.lineCap = 'round'
        context.beginPath()
        trajectory.forEach((frame, index) => {
          const [screenX, screenY] = toCanvas(frame.pose.x ?? centerX, frame.pose.y ?? centerY)
          if (index === 0) context.moveTo(screenX, screenY)
          else context.lineTo(screenX, screenY)
        })
        context.stroke()
      }

      const yawRadians = ((currentFrame.pose.yaw ?? 0) * Math.PI) / 180
      const arrowLength = 18
      const tipX = width / 2 + Math.cos(yawRadians) * arrowLength
      const tipY = height / 2 - Math.sin(yawRadians) * arrowLength
      context.fillStyle = '#f2b84b'
      context.strokeStyle = '#f2b84b'
      context.lineWidth = 2
      context.beginPath()
      context.arc(width / 2, height / 2, 5, 0, Math.PI * 2)
      context.fill()
      context.beginPath()
      context.moveTo(width / 2, height / 2)
      context.lineTo(tipX, tipY)
      context.stroke()
      context.beginPath()
      context.moveTo(tipX, tipY)
      context.lineTo(
        tipX - Math.cos(yawRadians - Math.PI / 5) * 7,
        tipY + Math.sin(yawRadians - Math.PI / 5) * 7,
      )
      context.lineTo(
        tipX - Math.cos(yawRadians + Math.PI / 5) * 7,
        tipY + Math.sin(yawRadians + Math.PI / 5) * 7,
      )
      context.closePath()
      context.fill()

      context.fillStyle = '#78909b'
      context.font = '500 11px ui-monospace, SFMono-Regular, monospace'
      context.textAlign = 'left'
      context.fillText(`${(halfHeight * 2).toFixed(1)} m`, 12, height - 12)
    }

    draw()
    const observer = new ResizeObserver(draw)
    observer.observe(canvas)
    return () => observer.disconnect()
  }, [currentFrame, trajectory])

  return (
    <section className="panel trajectory-panel" aria-label="最近三十秒轨迹">
      <div className="panel-heading panel-heading--compact">
        <div>
          <span className="eyebrow">LOCAL TRAJECTORY</span>
          <h2>最近 30 秒轨迹</h2>
        </div>
        <span className="panel-meta">当前位置居中 · 自动缩放</span>
      </div>
      <div className="trajectory-stage">
        <canvas ref={canvasRef} />
        <div className="trajectory-legend">
          <span><i className="legend-line" />历史轨迹</span>
          <span><i className="legend-arrow" />当前朝向</span>
        </div>
      </div>
    </section>
  )
}
