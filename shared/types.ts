export interface Pose {
  x: number | null
  y: number | null
  yaw: number | null
  valid: boolean
  poseAgeSeconds: number | null
}

export interface Velocity {
  vx: number | null
  vy: number | null
  wz: number | null
  linearVelocity: number | null
  angularVelocity: number | null
  source: string
  valid: boolean
}

export interface GridFrame {
  valid: boolean
  filename: string | null
  width: number | null
  height: number | null
  resolution: number | null
  originX: number | null
  originY: number | null
  frameId: string
}

export interface FrameSample {
  timestamp: number
  timestampMs: number
  dateTimeLocal: string
  pose: Pose
  velocity: Velocity
  grid: GridFrame
}

export interface CameraInfo {
  id: string
  positionZh: string
  positionEn: string
  videoUrl: string
  available: boolean
  coverage: number | null
  coverageWarning: string
}

export interface DatasetSummary {
  id: string
  taskId: string
  title: string
  robotId: string
  startedAtLocal: string
  endedAtLocal: string
  startedTimestamp: number | null
  endedTimestamp: number | null
  cameraIds: string[]
  frameCount: number
  complete: boolean
  warningCount: number
}

export interface DatasetDetail extends DatasetSummary {
  timeline: {
    from: number
    to: number
    duration: number
  }
  cameras: CameraInfo[]
  frames: FrameSample[]
  parseWarnings: string[]
}

export interface DatasetDeleteResult {
  deletedId: string
}

export interface DatasetTrimResult {
  id: string
  from: number
  to: number
  duration: number
  removedFrames: number
  removedGrids: number
  trimmedVideos: number
  warnings: string[]
}

export interface PlaybackState {
  currentTime: number
  duration: number
  isPlaying: boolean
}
