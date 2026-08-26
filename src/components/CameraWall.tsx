import { useCallback, useState } from 'react'
import type { CameraInfo } from '../../shared/types'

const CAMERA_POSITION_SLOTS = [
  ['后', '后左'],
  ['前上'],
  ['前广', '右中', '后上'],
  ['左'],
  ['前下'],
  ['右'],
] as const

function arrangeCameras(cameras: CameraInfo[]): Array<CameraInfo | null> {
  const unused = new Set(cameras.map((camera) => camera.id))
  const slots = CAMERA_POSITION_SLOTS.map((positionNames) => {
    const camera = cameras.find(
      (candidate) =>
        unused.has(candidate.id) && positionNames.some((positionName) => positionName === candidate.positionZh.trim()),
    ) ?? null
    if (camera) unused.delete(camera.id)
    return camera
  })

  const unassigned = cameras.filter((camera) => unused.has(camera.id))
  for (const slotIndex of [0, 2]) {
    if (!slots[slotIndex] && unassigned.length) slots[slotIndex] = unassigned.shift() ?? null
  }

  return slots
}

interface CameraTileProps {
  camera: CameraInfo | null
  registerVideo: (cameraId: string, video: HTMLVideoElement | null) => void
  onMediaError: (cameraId: string, failed: boolean) => void
  onBufferingChange: (cameraId: string, buffering: boolean) => void
  onPreviewFailure: () => void
  previewSessionId: string | null
  previewStartTime: number
  previewRevision: number
  initialTime: number
}

function CameraTile({
  camera,
  registerVideo,
  onMediaError,
  onBufferingChange,
  onPreviewFailure,
  previewSessionId,
  previewStartTime,
  previewRevision,
  initialTime,
}: CameraTileProps) {
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
    if (previewSessionId) {
      onPreviewFailure()
      return
    }
    setFailed(true)
    onMediaError(camera.id, true)
  }

  const sourceUrl = previewSessionId
    ? `${camera.videoUrl}/preview?session=${encodeURIComponent(previewSessionId)}&start=${previewStartTime.toFixed(3)}&revision=${previewRevision}`
    : camera.videoUrl

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
          key={sourceUrl}
          ref={attachVideo}
          src={sourceUrl}
          muted
          playsInline
          preload={previewSessionId ? 'auto' : 'metadata'}
          data-camera={camera.id}
          data-preview={previewSessionId ? 'true' : 'false'}
          data-stream-start={previewSessionId ? String(previewStartTime) : '0'}
          onLoadStart={() => onBufferingChange(camera.id, true)}
          onLoadedMetadata={(event) => {
            if (!previewSessionId && initialTime > 0) event.currentTarget.currentTime = initialTime
          }}
          onCanPlay={() => {
            onMediaError(camera.id, false)
            onBufferingChange(camera.id, false)
          }}
          onPlaying={() => onBufferingChange(camera.id, false)}
          onWaiting={() => onBufferingChange(camera.id, true)}
          onStalled={() => onBufferingChange(camera.id, true)}
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
  onBufferingChange?: (cameraId: string, buffering: boolean) => void
  onPreviewFailure?: () => void
  previewSessionId?: string | null
  previewStartTime?: number
  previewRevision?: number
  initialTime?: number
}

export function CameraWall({
  cameras,
  registerVideo,
  onMediaError,
  onBufferingChange = () => undefined,
  onPreviewFailure = () => undefined,
  previewSessionId = null,
  previewStartTime = 0,
  previewRevision = 0,
  initialTime = 0,
}: CameraWallProps) {
  const cameraSlots = arrangeCameras(cameras)
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
        {cameraSlots.map((camera, index) => (
          <CameraTile
            key={camera?.id ?? `empty-${index}`}
            camera={camera}
            registerVideo={registerVideo}
            onMediaError={onMediaError}
            onBufferingChange={onBufferingChange}
            onPreviewFailure={onPreviewFailure}
            previewSessionId={previewSessionId}
            previewStartTime={previewStartTime}
            previewRevision={previewRevision}
            initialTime={initialTime}
          />
        ))}
      </div>
    </section>
  )
}
