import type { FrameSample, MapName } from '../../shared/types'
import { formatFixed } from '../lib/playback'

interface ValueCardProps {
  label: string
  value: string
  unit: string
  accent?: boolean
}

function ValueCard({ label, value, unit, accent = false }: ValueCardProps) {
  return (
    <div className={`value-card${accent ? ' value-card--accent' : ''}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {unit ? <small>{unit}</small> : null}
    </div>
  )
}

export function TelemetryPanel({ frame, mapName }: { frame: FrameSample | null; mapName: MapName | null }) {
  const poseValid = frame?.pose.valid ?? false
  const velocityValid = frame?.velocity.valid ?? false
  return (
    <section className="panel telemetry-panel" aria-label="实时数据">
      <div className="panel-heading panel-heading--compact">
        <div>
          <span className="eyebrow">TELEMETRY</span>
          <h2>实时数据</h2>
        </div>
        <span
          className={`data-state${frame ? ' data-state--active' : ''}`}
          title={frame ? '预览使用当前视频时刻前后 1 秒内最近的 Meta' : ''}
        >
          <span className="status-dot" />
          {frame ? '近邻预览' : '无对应数据'}
        </span>
      </div>
      <div className="telemetry-group">
        <div className="group-label">
          <span>位姿</span>
          <span>map frame</span>
        </div>
        <div className="value-grid value-grid--pose">
          <ValueCard label="X" value={formatFixed(frame?.pose.x ?? null, poseValid)} unit="m" accent />
          <ValueCard label="Y" value={formatFixed(frame?.pose.y ?? null, poseValid)} unit="m" accent />
          <ValueCard label="YAW" value={formatFixed(frame?.pose.yaw ?? null, poseValid)} unit="deg" accent />
          <ValueCard label="map_name" value={mapName ?? '—'} unit="" accent={mapName !== null} />
        </div>
      </div>
      <div className="telemetry-group">
        <div className="group-label">
          <span>速度</span>
          <span className="velocity-source" title={frame?.velocity.source || ''}>
            {frame?.velocity.source || '—'}
          </span>
        </div>
        <div className="value-grid">
          <ValueCard label="VX" value={formatFixed(frame?.velocity.vx ?? null, velocityValid)} unit="m/s" />
          <ValueCard label="VY" value={formatFixed(frame?.velocity.vy ?? null, velocityValid)} unit="m/s" />
          <ValueCard label="WZ" value={formatFixed(frame?.velocity.wz ?? null, velocityValid)} unit="rad/s" />
        </div>
      </div>
    </section>
  )
}
