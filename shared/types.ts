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

export const MAP_NAMES = ['P_map', 'B9_map', 'B10_map', 'Lift_map'] as const

export type MapName = typeof MAP_NAMES[number]

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
  mapName: MapName | null
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

export interface DatasetMapNameResult {
  id: string
  mapName: MapName
  labeledFrames: number
}

export interface PlaybackState {
  currentTime: number
  duration: number
  isPlaying: boolean
}

export interface PreviewSessionResult {
  enabled: boolean
  mode: 'nvenc' | 'original'
  sessionId: string | null
  gpuIndex: number | null
  reason: string
  profile: {
    width: number
    fps: number
    bitrateKbps: number
  } | null
}

export type TrainingCaseStatus = 'accepted' | 'rejected'

export interface TrainingRunSummary {
  id: string
  taskId: string
  pipelineVersion: string
  mode: 'pilot' | 'full'
  name: string
  status: string
  completedAt: string
  candidateCount: number
  acceptedCount: number
  rejectedCount: number
  sourceCount: number
  routeCount: number
  subtaskIds: string[]
  rejectionReasons: Record<string, number>
  maxSyncMs: number
  maxRouteLateralMeters: number
  maxRouteTangentDegrees: number
  fullGenerationAuthorized: boolean
}

export interface TrainingCaseSummary {
  key: string
  caseId: string | null
  status: TrainingCaseStatus
  subtaskId: string
  rowIndex: number
  timestamp: number
  mapName: string
  routeId: string
  rejectReason: string | null
  collision: boolean
  maxSyncMs: number
  staticAddedCells: number
  commandCount: number
}

export interface TrainingCasePage {
  total: number
  offset: number
  limit: number
  items: TrainingCaseSummary[]
}

export interface TrainingCameraSample {
  id: string
  positionZh: string
  positionEn: string
  frameIndex: number
  deltaMs: number
  positionDeltaMeters: number
  yawDeltaDegrees: number
  url: string | null
}

export interface TrainingCommand {
  linearMps: number
  angularRps: number
  durationSeconds: number
}

export interface TrainingCaseDetail extends TrainingCaseSummary {
  graphName: string
  gridPose: { x: number; y: number; yawRadians: number } | null
  routeProjection: { distanceMeters: number; remainingMeters: number; progressMeters: number } | null
  initialState: {
    linearMps: number
    angularRps: number
    curvature: number
    indoor: boolean
  } | null
  occupancyFusion: {
    version: string
    rawObstacleCells: number
    staticObstacleCells: number
    staticAddedCells: number
    fusedObstacleCells: number
    sha256: string
  } | null
  rollout: {
    collision: boolean
    collisionStateIndex: number | null
    durationSeconds: number
  } | null
  teacher: {
    modelId: string
    profile: string
    inferenceMs: number | null
    commands: TrainingCommand[]
  } | null
  cameras: TrainingCameraSample[]
  mapRoute: {
    previewUrl: string
    archiveName: string
    sourceMapName: string
    sourceMapSha256: string
    cropRows: number
    cropColumns: number
    resolutionMeters: number
    extentMeters: number
    routePointCount: number
  } | null
  media: {
    rawGridUrl: string
    fusedGridUrl: string
  } | null
}
