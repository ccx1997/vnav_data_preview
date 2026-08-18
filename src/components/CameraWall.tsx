import { useCallback, useState } from 'react'
import type { CameraInfo } from '../../shared/types'

const CAMERA_SLOTS: Array<string | null> = [null, 'cam3', 'cam7', 'cam0', 'cam2', 'cam1']

interface CameraTileProps {
  camera: CameraInfo | null
  registerVideo: (cameraId: string, video: HTMLVideoElement | null) => void
  onMediaError: (cameraId: string, failed: boolean) => void
}

function CameraTile({ camera, registerVideo, onMediaError }: CameraTileProps) {
  const [failed, setFailed] = useState(false)
  const attachVideo = useCallback(
    (video: HTMLVideoElement | null) => {
      if (camera) registerVideo(camera.id, video)
    },
    [camera, registerVideo],
  )

  if (!camera) {
    return (
      <div className="camera-tile camera-tile--empty">
        <div className="empty-camera-icon" aria-hidden="true">
          +
        </div>
        <span>未配置第六路相机</span>
      </div>
    )
  }

  const markFailed = () => {
    setFailed(true)
    onMediaError(camera.id, true)
  }

  return (
    <div className="camera-tile">
      <div className="camera-label">
        <span>{camera.positionZh}</span>
        <code>{camera.id}</code>
      </div>
      {!camera.available || failed ? (
        <div className="media-placeholder media-placeholder--error">
          <span className="status-dot status-dot--error" />
          视频不可用
        </div>
      ) : (
        <video
          ref={attachVideo}
          src={camera.videoUrl}
          muted
          playsInline
          preload="metadata"
          data-camera={camera.id}
          onCanPlay={() => onMediaError(camera.id, false)}
          onError={markFailed}
        />
      )}
      {camera.coverage !== null && camera.coverage < 0.999 ? (
        <span className="coverage-badge" title={camera.coverageWarning}>
          覆盖 {(camera.coverage * 100).toFixed(1)}%
        </span>
      ) : null}
    </div>
  )
}

interface CameraWallProps {
  cameras: CameraInfo[]
  registerVideo: (cameraId: string, video: HTMLVideoElement | null) => void
  onMediaError: (cameraId: string, failed: boolean) => void
}

export function CameraWall({ cameras, registerVideo, onMediaError }: CameraWallProps) {
  const byId = new Map(cameras.map((camera) => [camera.id, camera]))
  return (
    <section className="panel camera-panel" aria-label="多视角相机">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">MULTI-CAMERA</span>
          <h2>多视角画面</h2>
        </div>
        <span className="panel-meta">2 × 3 同步预览</span>
      </div>
      <div className="camera-grid">
        {CAMERA_SLOTS.map((cameraId, index) => (
          <CameraTile
            key={cameraId ?? `empty-${index}`}
            camera={cameraId ? (byId.get(cameraId) ?? null) : null}
            registerVideo={registerVideo}
            onMediaError={onMediaError}
          />
        ))}
      </div>
    </section>
  )
}
