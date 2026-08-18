import { createReadStream, existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { basename, join, resolve } from 'node:path'
import type { Response } from 'express'
import type {
  CameraInfo,
  DatasetDetail,
  DatasetSummary,
  FrameSample,
  GridFrame,
  Pose,
  Velocity,
} from '../shared/types.js'

type JsonObject = Record<string, unknown>

interface DatasetDescriptor {
  id: string
  metaDirectory: string
  videoDirectory: string
  taskPath: string
  exportMetaPath: string
  framesPath: string
  manifestPath: string
  gridsDirectory: string
}

interface LoadedDataset {
  signature: string
  detail: DatasetDetail
  videos: Map<string, string>
  grids: Set<string>
}

const META_PREFIX = 'meta_'
const VIDEO_PREFIX = 'videos_'

function asObject(value: unknown): JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as JsonObject)
    : {}
}

function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function asBoolean(value: unknown, fallback = false): boolean {
  return typeof value === 'boolean' ? value : fallback
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function readJson(path: string): JsonObject {
  return asObject(JSON.parse(readFileSync(path, 'utf8')))
}

function fileSignature(paths: string[]): string {
  return paths
    .map((path) => {
      try {
        const stat = statSync(path)
        return `${stat.mtimeMs}:${stat.size}`
      } catch {
        return 'missing'
      }
    })
    .join('|')
}

function normalizePose(value: unknown): Pose {
  const pose = asObject(value)
  return {
    x: asNumber(pose.x),
    y: asNumber(pose.y),
    yaw: asNumber(pose.yaw),
    valid: asBoolean(pose.valid),
    poseAgeSeconds: asNumber(pose.pose_age_s),
  }
}

function normalizeVelocity(value: unknown): Velocity {
  const velocity = asObject(value)
  return {
    vx: asNumber(velocity.vx),
    vy: asNumber(velocity.vy),
    wz: asNumber(velocity.wz),
    linearVelocity: asNumber(velocity.linear_vel_mps),
    angularVelocity: asNumber(velocity.angular_vel_rads),
    source: asString(velocity.src, 'unknown'),
    valid: asBoolean(velocity.valid),
  }
}

function normalizeGrid(frame: JsonObject): GridFrame {
  const rawGridPath = asString(frame.grid_png)
  const filename = rawGridPath ? basename(rawGridPath) : null
  return {
    valid: asBoolean(frame.grid_valid) && filename !== null,
    filename,
    width: asNumber(frame.width),
    height: asNumber(frame.height),
    resolution: asNumber(frame.resolution),
    originX: asNumber(frame.origin_x),
    originY: asNumber(frame.origin_y),
    frameId: asString(frame.frame_id),
  }
}

export function parseFramesJsonl(content: string): {
  frames: FrameSample[]
  warnings: string[]
  grids: Set<string>
} {
  const frames: FrameSample[] = []
  const warnings: string[] = []
  const grids = new Set<string>()

  for (const [index, line] of content.split(/\r?\n/).entries()) {
    if (!line.trim()) continue
    try {
      const frame = asObject(JSON.parse(line))
      const timestamp = asNumber(frame.ts)
      if (timestamp === null) {
        warnings.push(`第 ${index + 1} 行缺少有效时间戳`)
        continue
      }
      const grid = normalizeGrid(frame)
      if (grid.filename) grids.add(grid.filename)
      frames.push({
        timestamp,
        timestampMs: asNumber(frame.ts_ms) ?? Math.round(timestamp * 1000),
        dateTimeLocal: asString(frame.datetime_local),
        pose: normalizePose(frame.pose),
        velocity: normalizeVelocity(frame.actual_vel),
        grid,
      })
    } catch {
      warnings.push(`第 ${index + 1} 行 JSON 无法解析`)
    }
  }

  frames.sort((left, right) => left.timestamp - right.timestamp)
  return { frames, warnings, grids }
}

export class DatasetRepository {
  readonly rootDirectory: string
  private readonly cache = new Map<string, LoadedDataset>()

  constructor(rootDirectory = resolve(process.cwd(), 'tmp_data')) {
    this.rootDirectory = resolve(rootDirectory)
  }

  private scan(): DatasetDescriptor[] {
    if (!existsSync(this.rootDirectory)) return []
    const entries = readdirSync(this.rootDirectory, { withFileTypes: true })
    const directoryNames = new Set(entries.filter((entry) => entry.isDirectory()).map((entry) => entry.name))
    const descriptors: DatasetDescriptor[] = []

    for (const entry of entries) {
      if (!entry.isDirectory() || !entry.name.startsWith(META_PREFIX)) continue
      const id = entry.name.slice(META_PREFIX.length)
      if (!id || !directoryNames.has(`${VIDEO_PREFIX}${id}`)) continue
      const metaDirectory = join(this.rootDirectory, entry.name)
      const videoDirectory = join(this.rootDirectory, `${VIDEO_PREFIX}${id}`)
      descriptors.push({
        id,
        metaDirectory,
        videoDirectory,
        taskPath: join(metaDirectory, 'task.json'),
        exportMetaPath: join(metaDirectory, 'export_meta.json'),
        framesPath: join(metaDirectory, 'frames.jsonl'),
        manifestPath: join(videoDirectory, 'manifest.json'),
        gridsDirectory: join(metaDirectory, 'grids'),
      })
    }
    return descriptors
  }

  private getDescriptor(id: string): DatasetDescriptor | null {
    return this.scan().find((descriptor) => descriptor.id === id) ?? null
  }

  list(): DatasetSummary[] {
    return this.scan()
      .map((descriptor) => this.readSummary(descriptor))
      .sort((left, right) => (right.startedTimestamp ?? 0) - (left.startedTimestamp ?? 0))
  }

  private readSummary(descriptor: DatasetDescriptor): DatasetSummary {
    const warnings: string[] = []
    let task: JsonObject = {}
    let manifest: JsonObject = {}
    let exportMeta: JsonObject = {}

    for (const [path, assign] of [
      [descriptor.taskPath, (value: JsonObject) => (task = value)],
      [descriptor.manifestPath, (value: JsonObject) => (manifest = value)],
      [descriptor.exportMetaPath, (value: JsonObject) => (exportMeta = value)],
    ] as const) {
      try {
        assign(readJson(path))
      } catch {
        warnings.push(`无法读取 ${basename(path)}`)
      }
    }

    const cameraIds = asStringArray(manifest.cameras).length
      ? asStringArray(manifest.cameras)
      : asStringArray(task.video_cameras)
    const perCamera = asObject(manifest.per_camera)
    const missingVideos = cameraIds.filter((camera) => {
      const cameraManifest = asObject(perCamera[camera])
      const filename = asString(cameraManifest.file, `${camera}.mp4`)
      return !existsSync(join(descriptor.videoDirectory, filename))
    })
    warnings.push(...missingVideos.map((camera) => `缺少 ${camera} 视频`))
    if (!existsSync(descriptor.framesPath)) warnings.push('缺少 frames.jsonl')

    return {
      id: descriptor.id,
      taskId: asString(task.task_id, asString(manifest.task_id, descriptor.id)),
      title: asString(task.title, descriptor.id),
      robotId: asString(task.robot_id, asString(manifest.robot)),
      startedAtLocal: asString(task.started_at_local),
      endedAtLocal: asString(task.ended_at_local),
      startedTimestamp: asNumber(task.started_ts) ?? asNumber(manifest.from),
      endedTimestamp: asNumber(task.ended_ts) ?? asNumber(manifest.to),
      cameraIds,
      frameCount: asNumber(exportMeta.sample_count) ?? 0,
      complete: warnings.length === 0 && !asBoolean(manifest.partial),
      warningCount: warnings.length,
    }
  }

  load(id: string): LoadedDataset | null {
    const descriptor = this.getDescriptor(id)
    if (!descriptor) return null
    const signature = fileSignature([
      descriptor.taskPath,
      descriptor.exportMetaPath,
      descriptor.framesPath,
      descriptor.manifestPath,
    ])
    const cached = this.cache.get(id)
    if (cached?.signature === signature) return cached

    const summary = this.readSummary(descriptor)
    const task = readJson(descriptor.taskPath)
    const manifest = readJson(descriptor.manifestPath)
    const parsed = parseFramesJsonl(readFileSync(descriptor.framesPath, 'utf8'))
    const perCamera = asObject(manifest.per_camera)
    const cameraPositions = asObject(task.camera_positions)
    const videos = new Map<string, string>()

    const cameras: CameraInfo[] = summary.cameraIds.map((cameraId) => {
      const cameraManifest = asObject(perCamera[cameraId])
      const position = asObject(cameraPositions[cameraId])
      const filename = asString(cameraManifest.file, `${cameraId}.mp4`)
      const videoPath = join(descriptor.videoDirectory, filename)
      const available = existsSync(videoPath)
      if (available) videos.set(cameraId, videoPath)
      return {
        id: cameraId,
        positionZh: asString(position.position_zh, cameraId),
        positionEn: asString(position.position_en),
        videoUrl: `/media/${encodeURIComponent(id)}/cameras/${encodeURIComponent(cameraId)}`,
        available,
        coverage: asNumber(cameraManifest.coverage),
        coverageWarning: asString(cameraManifest.coverage_warning),
      }
    })

    const from = asNumber(manifest.from) ?? summary.startedTimestamp ?? parsed.frames[0]?.timestamp ?? 0
    const to = asNumber(manifest.to) ?? summary.endedTimestamp ?? parsed.frames.at(-1)?.timestamp ?? from
    const gridMissingCount = [...parsed.grids].filter(
      (filename) => !existsSync(join(descriptor.gridsDirectory, filename)),
    ).length
    const parseWarnings = [...parsed.warnings]
    if (gridMissingCount) parseWarnings.push(`缺少 ${gridMissingCount} 张占据图`)

    const detail: DatasetDetail = {
      ...summary,
      frameCount: parsed.frames.length,
      complete: summary.complete && parseWarnings.length === 0,
      warningCount: summary.warningCount + parseWarnings.length,
      timeline: { from, to, duration: Math.max(0, to - from) },
      cameras,
      frames: parsed.frames,
      parseWarnings,
    }
    const loaded = { signature, detail, videos, grids: parsed.grids }
    this.cache.set(id, loaded)
    return loaded
  }

  getCameraPath(id: string, cameraId: string): string | null {
    return this.load(id)?.videos.get(cameraId) ?? null
  }

  getGridPath(id: string, filename: string): string | null {
    if (basename(filename) !== filename || !/^\d+\.png$/.test(filename)) return null
    const loaded = this.load(id)
    if (!loaded?.grids.has(filename)) return null
    const descriptor = this.getDescriptor(id)
    if (!descriptor) return null
    const path = join(descriptor.gridsDirectory, filename)
    return existsSync(path) ? path : null
  }
}

export function sendVideoWithRange(response: Response, videoPath: string, rangeHeader?: string): void {
  const stat = statSync(videoPath)
  const total = stat.size
  response.setHeader('Accept-Ranges', 'bytes')
  response.setHeader('Content-Type', 'video/mp4')
  response.setHeader('Cache-Control', 'public, max-age=3600')

  if (!rangeHeader) {
    response.setHeader('Content-Length', total)
    createReadStream(videoPath).pipe(response)
    return
  }

  const byteRange = resolveByteRange(rangeHeader, total)
  if (!byteRange) {
    response.status(416).setHeader('Content-Range', `bytes */${total}`).end()
    return
  }
  const { start, end } = byteRange

  response.status(206)
  response.setHeader('Content-Range', `bytes ${start}-${end}/${total}`)
  response.setHeader('Content-Length', end - start + 1)
  createReadStream(videoPath, { start, end }).pipe(response)
}

export function resolveByteRange(rangeHeader: string, total: number): { start: number; end: number } | null {
  const match = /^bytes=(\d*)-(\d*)$/.exec(rangeHeader)
  if (!match || (!match[1] && !match[2]) || total <= 0) return null

  if (!match[1]) {
    const suffixLength = Number(match[2])
    if (!Number.isFinite(suffixLength) || suffixLength <= 0) return null
    return { start: Math.max(0, total - suffixLength), end: total - 1 }
  }

  const start = Number(match[1])
  const end = match[2] ? Math.min(Number(match[2]), total - 1) : total - 1
  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || start > end || start >= total) return null
  return { start, end }
}
