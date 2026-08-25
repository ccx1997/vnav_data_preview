import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { createReadStream, existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { rename, rm, stat, unlink, writeFile } from 'node:fs/promises'
import { basename, dirname, join, resolve } from 'node:path'
import type { Response } from 'express'
import type {
  CameraInfo,
  DatasetDeleteResult,
  DatasetDetail,
  DatasetSummary,
  DatasetTrimResult,
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
  grouped: boolean
  taskPath: string
  exportMetaPath: string
  framesPath: string
  videoMetadataPath: string
  defaultVideoSuffix: string
  gridsDirectory: string
}

interface LoadedDataset {
  signature: string
  detail: DatasetDetail
  videos: Map<string, string>
  grids: Set<string>
}

type VideoTrimmer = (
  inputPath: string,
  outputPath: string,
  startSeconds: number,
  durationSeconds: number,
) => Promise<void>

interface DatasetRepositoryOptions {
  trimVideo?: VideoTrimmer
}

interface RawFrameLine {
  line: string
  value: JsonObject
  timestamp: number
  gridFilename: string | null
}

const META_PREFIX = 'meta_'
const VIDEO_PREFIX = 'videos_'
const DEFAULT_VISUAL_NAV_ROOT = '/mnt/chengchangxu/data/visual_nav_mv'

function runFfmpeg(inputPath: string, outputPath: string, startSeconds: number, durationSeconds: number): Promise<void> {
  const ffmpeg = process.env.FFMPEG_PATH ?? 'ffmpeg'
  const args = [
    '-hide_banner',
    '-loglevel',
    'error',
    '-nostdin',
    '-y',
    '-ss',
    startSeconds.toFixed(6),
    '-i',
    inputPath,
    '-t',
    durationSeconds.toFixed(6),
    '-map',
    '0:v:0',
    '-an',
    '-vf',
    'setpts=PTS-STARTPTS',
    '-c:v',
    'libx264',
    '-preset',
    'veryfast',
    '-crf',
    '18',
    '-pix_fmt',
    'yuv420p',
    '-movflags',
    '+faststart',
    outputPath,
  ]

  return new Promise((resolvePromise, reject) => {
    const child = spawn(ffmpeg, args, { stdio: ['ignore', 'ignore', 'pipe'] })
    let stderr = ''
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', (chunk: string) => {
      stderr = `${stderr}${chunk}`.slice(-4000)
    })
    child.on('error', (error) => {
      reject(new Error(error.message.includes('ENOENT') ? '未找到 ffmpeg，无法裁剪视频' : `无法启动 ffmpeg：${error.message}`))
    })
    child.on('close', (code) => {
      if (code === 0) resolvePromise()
      else reject(new Error(`视频裁剪失败${stderr.trim() ? `：${stderr.trim()}` : `（ffmpeg ${code ?? 'unknown'}）`}`))
    })
  })
}

async function runWithConcurrency<T>(items: T[], concurrency: number, operation: (item: T) => Promise<void>) {
  let nextIndex = 0
  const workers = Array.from({ length: Math.min(concurrency, items.length) }, async () => {
    while (nextIndex < items.length) {
      const item = items[nextIndex]
      nextIndex += 1
      await operation(item)
    }
  })
  await Promise.all(workers)
}

function parseRawFrameLines(content: string): { valid: RawFrameLine[]; passthrough: string[] } {
  const valid: RawFrameLine[] = []
  const passthrough: string[] = []
  for (const line of content.split(/\r?\n/)) {
    if (!line.trim()) continue
    try {
      const value = asObject(JSON.parse(line))
      const timestamp = asNumber(value.ts)
      if (timestamp === null) {
        passthrough.push(line)
        continue
      }
      const rawGridPath = asString(value.grid_png)
      valid.push({
        line,
        value,
        timestamp,
        gridFilename: rawGridPath ? basename(rawGridPath) : null,
      })
    } catch {
      passthrough.push(line)
    }
  }
  return { valid, passthrough }
}

function timestampFields(timestamp: number, timezone: string): { utc: string; local: string } {
  const date = new Date(timestamp * 1000)
  const utc = date.toISOString()
  try {
    const local = new Intl.DateTimeFormat('sv-SE', {
      timeZone: timezone || 'Asia/Shanghai',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      fractionalSecondDigits: 3,
      hourCycle: 'h23',
    }).format(date).replace(',', '.')
    return { utc, local }
  } catch {
    return { utc, local: utc.replace('T', ' ').replace('Z', '') }
  }
}

function updateWindow(value: JsonObject, from: number, to: number): void {
  value.from = from
  value.to = to
  value.keep_windows = [{ from, to }]
}

function updateTaskMetadata(
  task: JsonObject,
  descriptor: DatasetDescriptor,
  from: number,
  to: number,
  videoSizes: Map<string, number>,
): void {
  const timezone = asString(task.tz, 'Asia/Shanghai')
  const startedAt = timestampFields(from, timezone)
  const endedAt = timestampFields(to, timezone)
  task.started_ts = from
  task.ended_ts = to
  task.started_at_utc = startedAt.utc
  task.started_at_local = startedAt.local
  task.ended_at_utc = endedAt.utc
  task.ended_at_local = endedAt.local

  const subTask = asObject(task.sub_task)
  if (descriptor.grouped || Object.keys(subTask).length) {
    const previousFrom = asNumber(subTask.from)
    const previousTo = asNumber(subTask.to)
    updateWindow(subTask, from, to)
    task.sub_task = subTask
    if (Array.isArray(task.keep_windows)) {
      task.keep_windows = task.keep_windows.map((entry) => {
        const window = asObject(entry)
        return asNumber(window.from) === previousFrom && asNumber(window.to) === previousTo ? { from, to } : entry
      })
    }
    if (Array.isArray(task.sub_tasks)) {
      task.sub_tasks = task.sub_tasks.map((entry) => {
        const candidate = asObject(entry)
        return asString(candidate.sub_task_id) === descriptor.id
          ? { ...candidate, from, to, keep_windows: [{ from, to }] }
          : entry
      })
    }
  }

  const videoContinuous = asObject(task.video_continuous)
  if (Array.isArray(videoContinuous.items)) {
    videoContinuous.items = videoContinuous.items.map((entry) => {
      const item = asObject(entry)
      if (asString(item.sub_task_id) !== descriptor.id) return entry
      const size = videoSizes.get(asString(item.camera))
      return { ...item, ...(size === undefined ? {} : { bytes: size }), locally_trimmed: true }
    })
    task.video_continuous = videoContinuous
  }
  task.local_trim = { from, to, updated_at: new Date().toISOString() }
}

function updateExportMetadata(
  exportMeta: JsonObject,
  descriptor: DatasetDescriptor,
  keptFrames: RawFrameLine[],
  from: number,
  to: number,
): void {
  const gridFrames = keptFrames.filter((frame) => asBoolean(frame.value.grid_valid) && frame.gridFilename)
  exportMeta.sample_count = keptFrames.length
  exportMeta.grids_total = gridFrames.length
  exportMeta.grids_valid = gridFrames.length
  exportMeta.grids_with_pose = gridFrames.filter((frame) => asBoolean(asObject(frame.value.pose).valid)).length
  exportMeta.png_rendered = gridFrames.length
  exportMeta.pose_count = keptFrames.filter((frame) => asBoolean(asObject(frame.value.pose).valid)).length
  exportMeta.vel_count = keptFrames.filter((frame) => asBoolean(asObject(frame.value.actual_vel).valid)).length
  exportMeta.keep_windows = [{ from, to }]
  if (descriptor.grouped || Object.keys(asObject(exportMeta.sub_task)).length) {
    const subTask = asObject(exportMeta.sub_task)
    updateWindow(subTask, from, to)
    exportMeta.sub_task = subTask
  }
  exportMeta.local_trim = { from, to, updated_at: new Date().toISOString() }
}

function updateVideoMetadata(
  metadata: JsonObject,
  descriptor: DatasetDescriptor,
  from: number,
  to: number,
  videoSizes: Map<string, number>,
): void {
  const duration = to - from
  if (!descriptor.grouped) {
    metadata.from = from
    metadata.to = to
    const perCamera = asObject(metadata.per_camera)
    for (const [cameraId, size] of videoSizes) {
      const camera = asObject(perCamera[cameraId])
      camera.output_window = { from, to }
      camera.window_s = duration
      camera.output_bytes = size
      camera.coverage = null
      camera.coverage_warning = '本地裁剪后未重新计算覆盖率'
      perCamera[cameraId] = camera
    }
    metadata.per_camera = perCamera
  } else if (Array.isArray(metadata.segments)) {
    const segments = metadata.segments.flatMap((entry) => {
      const segment = asObject(entry)
      const segmentFrom = asNumber(segment.clip_from_ts) ?? asNumber(segment.start_ts)
      const segmentTo = asNumber(segment.clip_to_ts) ?? asNumber(segment.end_ts)
      if (segmentFrom === null || segmentTo === null || segmentTo < from || segmentFrom > to) return []
      return [{
        ...segment,
        clip_from_ts: Math.max(from, segmentFrom),
        clip_to_ts: Math.min(to, segmentTo),
        keep_windows: [{ from, to }],
      }]
    })
    metadata.segments = segments
    metadata.count = segments.length
  }
  metadata.local_trim = { from, to, updated_at: new Date().toISOString() }
}

function createDescriptor(
  id: string,
  metaDirectory: string,
  videoDirectory: string,
  grouped: boolean,
): DatasetDescriptor {
  const manifestPath = join(videoDirectory, 'manifest.json')
  const videoSegmentsPath = join(metaDirectory, 'video_segments.json')
  const usesVideoSegments = !existsSync(manifestPath) && existsSync(videoSegmentsPath)

  return {
    id,
    metaDirectory,
    videoDirectory,
    grouped,
    taskPath: join(metaDirectory, 'task.json'),
    exportMetaPath: join(metaDirectory, 'export_meta.json'),
    framesPath: join(metaDirectory, 'frames.jsonl'),
    videoMetadataPath: usesVideoSegments ? videoSegmentsPath : manifestPath,
    defaultVideoSuffix: usesVideoSegments ? '_continuous.mp4' : '.mp4',
    gridsDirectory: join(metaDirectory, 'grids'),
  }
}

function looksLikeDataset(metaDirectory: string, videoDirectory: string): boolean {
  return [
    join(metaDirectory, 'task.json'),
    join(metaDirectory, 'frames.jsonl'),
    join(metaDirectory, 'video_segments.json'),
    join(videoDirectory, 'manifest.json'),
  ].some(existsSync)
}

function videoFilename(descriptor: DatasetDescriptor, metadata: JsonObject, cameraId: string): string {
  const cameraMetadata = asObject(asObject(metadata.per_camera)[cameraId])
  const explicitFilename = asString(cameraMetadata.file)
  if (explicitFilename) return basename(explicitFilename)

  const result = Array.isArray(metadata.results)
    ? metadata.results.map(asObject).find((item) => asString(item.camera) === cameraId)
    : undefined
  const resultPath = asString(result?.out)
  if (resultPath) return basename(resultPath)

  const continuousFilename = `${cameraId}_continuous.mp4`
  return existsSync(join(descriptor.videoDirectory, continuousFilename))
    ? continuousFilename
    : `${cameraId}${descriptor.defaultVideoSuffix}`
}

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
  readonly rootDirectories: string[]
  private readonly cache = new Map<string, LoadedDataset>()
  private readonly activeOperations = new Set<string>()
  private readonly trimVideo: VideoTrimmer

  constructor(rootDirectory?: string | string[], options: DatasetRepositoryOptions = {}) {
    const configuredRoots = rootDirectory === undefined
      ? [resolve(process.cwd(), 'tmp_data'), DEFAULT_VISUAL_NAV_ROOT]
      : Array.isArray(rootDirectory)
        ? rootDirectory
        : [rootDirectory]
    this.rootDirectories = [...new Set(configuredRoots.map((directory) => resolve(directory)))]
    this.rootDirectory = this.rootDirectories[0]
    this.trimVideo = options.trimVideo ?? runFfmpeg
  }

  private scanPairedDirectories(
    metaRoot: string,
    videoRoot: string,
    descriptors: Map<string, DatasetDescriptor>,
    grouped: boolean,
  ): void {
    if (!existsSync(metaRoot) || !existsSync(videoRoot)) return
    const metaEntries = readdirSync(metaRoot, { withFileTypes: true }).sort((left, right) =>
      left.name.localeCompare(right.name),
    )
    const videoNames = new Set(
      readdirSync(videoRoot, { withFileTypes: true })
        .filter((entry) => entry.isDirectory())
        .map((entry) => entry.name),
    )

    for (const entry of metaEntries) {
      if (!entry.isDirectory() || !entry.name.startsWith(META_PREFIX)) continue
      const id = entry.name.slice(META_PREFIX.length)
      if (!id) continue
      const videoName = videoNames.has(`${VIDEO_PREFIX}${id}`)
        ? `${VIDEO_PREFIX}${id}`
        : videoNames.has(id)
          ? id
          : null
      if (!videoName) continue
      const metaDirectory = join(metaRoot, entry.name)
      const videoDirectory = join(videoRoot, videoName)

      if (looksLikeDataset(metaDirectory, videoDirectory)) {
        if (!descriptors.has(id)) {
          descriptors.set(id, createDescriptor(id, metaDirectory, videoDirectory, grouped))
        }
      }
    }
  }

  private scanConventionalRoot(rootDirectory: string, descriptors: Map<string, DatasetDescriptor>): void {
    const entries = readdirSync(rootDirectory, { withFileTypes: true }).sort((left, right) =>
      left.name.localeCompare(right.name),
    )
    const directoryNames = new Set(entries.filter((entry) => entry.isDirectory()).map((entry) => entry.name))

    this.scanPairedDirectories(rootDirectory, rootDirectory, descriptors, false)

    for (const entry of entries) {
      if (!entry.isDirectory() || !entry.name.startsWith(META_PREFIX)) continue
      const containerId = entry.name.slice(META_PREFIX.length)
      if (!containerId || !directoryNames.has(`${VIDEO_PREFIX}${containerId}`)) continue
      const metaDirectory = join(rootDirectory, entry.name)
      const videoDirectory = join(rootDirectory, `${VIDEO_PREFIX}${containerId}`)

      const nestedMetaEntries = readdirSync(metaDirectory, { withFileTypes: true }).sort((left, right) =>
        left.name.localeCompare(right.name),
      )
      const nestedVideoNames = new Set(
        readdirSync(videoDirectory, { withFileTypes: true })
          .filter((nestedEntry) => nestedEntry.isDirectory())
          .map((nestedEntry) => nestedEntry.name),
      )

      for (const nestedEntry of nestedMetaEntries) {
        if (!nestedEntry.isDirectory() || !nestedEntry.name.startsWith(META_PREFIX)) continue
        const id = nestedEntry.name.slice(META_PREFIX.length)
        if (!id) continue
        const nestedVideoName = nestedVideoNames.has(id)
          ? id
          : nestedVideoNames.has(`${VIDEO_PREFIX}${id}`)
            ? `${VIDEO_PREFIX}${id}`
            : null
        if (!nestedVideoName) continue

        const nestedMetaDirectory = join(metaDirectory, nestedEntry.name)
        const nestedVideoDirectory = join(videoDirectory, nestedVideoName)
        if (!looksLikeDataset(nestedMetaDirectory, nestedVideoDirectory)) continue
        if (!descriptors.has(id)) {
          descriptors.set(id, createDescriptor(id, nestedMetaDirectory, nestedVideoDirectory, true))
        }
      }
    }
  }

  private scanVisualNavRoot(rootDirectory: string, descriptors: Map<string, DatasetDescriptor>): void {
    const taskEntries = readdirSync(rootDirectory, { withFileTypes: true }).sort((left, right) =>
      left.name.localeCompare(right.name),
    )
    for (const taskEntry of taskEntries) {
      if (!taskEntry.isDirectory()) continue
      const taskDirectory = join(rootDirectory, taskEntry.name)
      this.scanPairedDirectories(
        join(taskDirectory, 'meta', 'unpacked'),
        join(taskDirectory, 'videos'),
        descriptors,
        true,
      )
    }
  }

  private scan(): DatasetDescriptor[] {
    const descriptors = new Map<string, DatasetDescriptor>()
    for (const rootDirectory of this.rootDirectories) {
      if (!existsSync(rootDirectory)) continue
      this.scanConventionalRoot(rootDirectory, descriptors)
      this.scanVisualNavRoot(rootDirectory, descriptors)
    }
    return [...descriptors.values()]
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
      [descriptor.videoMetadataPath, (value: JsonObject) => (manifest = value)],
      [descriptor.exportMetaPath, (value: JsonObject) => (exportMeta = value)],
    ] as const) {
      try {
        assign(readJson(path))
      } catch {
        warnings.push(`无法读取 ${basename(path)}`)
      }
    }

    const manifestCameras = asStringArray(manifest.cameras)
    const segmentCameras = asStringArray(manifest.video_cameras)
    const cameraIds = manifestCameras.length
      ? manifestCameras
      : segmentCameras.length
        ? segmentCameras
        : asStringArray(task.video_cameras)
    const missingVideos = cameraIds.filter((camera) => {
      const filename = videoFilename(descriptor, manifest, camera)
      return !existsSync(join(descriptor.videoDirectory, filename))
    })
    warnings.push(...missingVideos.map((camera) => `缺少 ${camera} 视频`))
    if (!existsSync(descriptor.framesPath)) warnings.push('缺少 frames.jsonl')

    const exportSubTask = asObject(exportMeta.sub_task)
    const taskSubTask = asObject(task.sub_task)
    const subTaskId = asString(exportSubTask.sub_task_id, asString(taskSubTask.sub_task_id, descriptor.id))
    const subTaskIndex = asNumber(exportSubTask.index) ?? asNumber(taskSubTask.index)
    const baseTitle = asString(task.title, descriptor.id)

    return {
      id: descriptor.id,
      taskId: descriptor.grouped ? subTaskId : asString(task.task_id, asString(manifest.task_id, descriptor.id)),
      title: descriptor.grouped
        ? `${baseTitle} · 片段 ${subTaskIndex ?? subTaskId}`
        : baseTitle,
      robotId: asString(task.robot_id, asString(manifest.robot)),
      startedAtLocal: asString(task.started_at_local),
      endedAtLocal: asString(task.ended_at_local),
      startedTimestamp:
        asNumber(exportSubTask.from) ?? asNumber(taskSubTask.from) ?? asNumber(task.started_ts) ?? asNumber(manifest.from),
      endedTimestamp:
        asNumber(exportSubTask.to) ?? asNumber(taskSubTask.to) ?? asNumber(task.ended_ts) ?? asNumber(manifest.to),
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
      descriptor.videoMetadataPath,
    ])
    const cached = this.cache.get(id)
    if (cached?.signature === signature) return cached

    const summary = this.readSummary(descriptor)
    const task = readJson(descriptor.taskPath)
    const manifest = readJson(descriptor.videoMetadataPath)
    const exportMeta = readJson(descriptor.exportMetaPath)
    const parsed = parseFramesJsonl(readFileSync(descriptor.framesPath, 'utf8'))
    const perCamera = asObject(manifest.per_camera)
    const cameraPositions = asObject(task.camera_positions)
    const videos = new Map<string, string>()

    const cameras: CameraInfo[] = summary.cameraIds.map((cameraId) => {
      const cameraManifest = asObject(perCamera[cameraId])
      const position = asObject(cameraPositions[cameraId])
      const filename = videoFilename(descriptor, manifest, cameraId)
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

    const exportSubTask = asObject(exportMeta.sub_task)
    const taskSubTask = asObject(task.sub_task)
    const from =
      asNumber(manifest.from) ??
      asNumber(exportSubTask.from) ??
      asNumber(taskSubTask.from) ??
      summary.startedTimestamp ??
      parsed.frames[0]?.timestamp ??
      0
    const to =
      asNumber(manifest.to) ??
      asNumber(exportSubTask.to) ??
      asNumber(taskSubTask.to) ??
      summary.endedTimestamp ??
      parsed.frames.at(-1)?.timestamp ??
      from
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

  async delete(id: string): Promise<DatasetDeleteResult | null> {
    const descriptor = this.getDescriptor(id)
    if (!descriptor) return null
    if (this.activeOperations.has(id)) throw new Error('该数据集正在处理，请稍后再试')
    this.activeOperations.add(id)
    const token = randomUUID()
    const stagedMeta = join(dirname(descriptor.metaDirectory), `.deleting-${token}-meta`)
    const stagedVideo = join(dirname(descriptor.videoDirectory), `.deleting-${token}-video`)
    let metaStaged = false
    let videoStaged = false

    try {
      await rename(descriptor.metaDirectory, stagedMeta)
      metaStaged = true
      await rename(descriptor.videoDirectory, stagedVideo)
      videoStaged = true
      this.cache.delete(id)
      await Promise.all([
        rm(stagedMeta, { recursive: true, force: false }),
        rm(stagedVideo, { recursive: true, force: false }),
      ])
      return { deletedId: id }
    } catch (error) {
      if (videoStaged && existsSync(stagedVideo) && !existsSync(descriptor.videoDirectory)) {
        await rename(stagedVideo, descriptor.videoDirectory).catch(() => undefined)
      }
      if (metaStaged && existsSync(stagedMeta) && !existsSync(descriptor.metaDirectory)) {
        await rename(stagedMeta, descriptor.metaDirectory).catch(() => undefined)
      }
      throw error
    } finally {
      this.activeOperations.delete(id)
    }
  }

  async trim(id: string, trimStart: number, trimEnd: number): Promise<DatasetTrimResult | null> {
    const descriptor = this.getDescriptor(id)
    if (!descriptor) return null
    if (this.activeOperations.has(id)) throw new Error('该数据集正在处理，请稍后再试')
    if (!Number.isFinite(trimStart) || !Number.isFinite(trimEnd) || trimStart < 0 || trimEnd < 0) {
      throw new Error('开头和结尾的删除时长必须是大于或等于 0 的数字')
    }
    if (trimStart === 0 && trimEnd === 0) throw new Error('请至少填写一个需要删除的时长')

    const loaded = this.load(id)
    if (!loaded) return null
    const currentDuration = loaded.detail.timeline.duration
    const keptDuration = currentDuration - trimStart - trimEnd
    if (keptDuration < 0.1) throw new Error('删除时长过长，数据集至少需要保留 0.1 秒')

    this.activeOperations.add(id)
    const token = randomUUID()
    const replacements: Array<{ target: string; temporary: string; backup: string }> = []
    const temporaryFiles: string[] = []
    const newFrom = loaded.detail.timeline.from + trimStart
    const newTo = loaded.detail.timeline.to - trimEnd
    const rawFrames = parseRawFrameLines(readFileSync(descriptor.framesPath, 'utf8'))
    const keptFrames = rawFrames.valid.filter((frame) => frame.timestamp >= newFrom && frame.timestamp <= newTo)
    const removedFrames = rawFrames.valid.filter((frame) => frame.timestamp < newFrom || frame.timestamp > newTo)
    if (!keptFrames.length) {
      this.activeOperations.delete(id)
      throw new Error('所选范围内没有可保留的 Meta 帧，请缩短删除时长')
    }

    const keptGridNames = new Set(keptFrames.flatMap((frame) => frame.gridFilename ? [frame.gridFilename] : []))
    const removedGridNames = new Set(
      removedFrames.flatMap((frame) => frame.gridFilename && !keptGridNames.has(frame.gridFilename) ? [frame.gridFilename] : []),
    )

    try {
      const videos = [...loaded.videos.entries()].map(([cameraId, inputPath]) => {
        const temporary = join(descriptor.videoDirectory, `.${basename(inputPath)}.trim-${token}.mp4`)
        temporaryFiles.push(temporary)
        replacements.push({ target: inputPath, temporary, backup: `${inputPath}.backup-${token}` })
        return { cameraId, inputPath, temporary }
      })
      if (!videos.length) throw new Error('该数据集没有可裁剪的视频')

      await runWithConcurrency(videos, 2, async (video) => {
        await this.trimVideo(video.inputPath, video.temporary, trimStart, keptDuration)
      })

      const videoSizes = new Map<string, number>()
      for (const video of videos) videoSizes.set(video.cameraId, (await stat(video.temporary)).size)

      const task = readJson(descriptor.taskPath)
      const exportMeta = readJson(descriptor.exportMetaPath)
      const videoMetadata = readJson(descriptor.videoMetadataPath)
      updateTaskMetadata(task, descriptor, newFrom, newTo, videoSizes)
      updateExportMetadata(exportMeta, descriptor, keptFrames, newFrom, newTo)
      updateVideoMetadata(videoMetadata, descriptor, newFrom, newTo, videoSizes)

      const metadataReplacements = [
        { target: descriptor.taskPath, content: `${JSON.stringify(task, null, 2)}\n` },
        { target: descriptor.exportMetaPath, content: `${JSON.stringify(exportMeta, null, 2)}\n` },
        { target: descriptor.videoMetadataPath, content: `${JSON.stringify(videoMetadata, null, 2)}\n` },
        {
          target: descriptor.framesPath,
          content: `${[...keptFrames.map((frame) => frame.line), ...rawFrames.passthrough].join('\n')}\n`,
        },
      ]
      for (const item of metadataReplacements) {
        const temporary = `${item.target}.trim-${token}`
        await writeFile(temporary, item.content)
        temporaryFiles.push(temporary)
        replacements.push({ target: item.target, temporary, backup: `${item.target}.backup-${token}` })
      }

      const backedUp: typeof replacements = []
      const installed: typeof replacements = []
      try {
        for (const replacement of replacements) {
          await rename(replacement.target, replacement.backup)
          backedUp.push(replacement)
        }
        for (const replacement of replacements) {
          await rename(replacement.temporary, replacement.target)
          installed.push(replacement)
        }
      } catch (error) {
        for (const replacement of installed.reverse()) {
          await rm(replacement.target, { force: true }).catch(() => undefined)
        }
        for (const replacement of backedUp.reverse()) {
          if (existsSync(replacement.backup)) await rename(replacement.backup, replacement.target).catch(() => undefined)
        }
        throw error
      }

      const warnings: string[] = []
      for (const replacement of replacements) {
        try {
          await rm(replacement.backup, { force: true })
        } catch {
          warnings.push(`无法清理裁剪备份 ${basename(replacement.backup)}`)
        }
      }

      let removedGrids = 0
      for (const filename of removedGridNames) {
        if (basename(filename) !== filename) continue
        try {
          await unlink(join(descriptor.gridsDirectory, filename))
          removedGrids += 1
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== 'ENOENT') warnings.push(`无法删除占据图 ${filename}`)
        }
      }

      this.cache.delete(id)
      return {
        id,
        from: newFrom,
        to: newTo,
        duration: keptDuration,
        removedFrames: removedFrames.length,
        removedGrids,
        trimmedVideos: videos.length,
        warnings,
      }
    } finally {
      await Promise.all(temporaryFiles.map((path) => rm(path, { force: true }).catch(() => undefined)))
      this.activeOperations.delete(id)
    }
  }
}

export function sendVideoWithRange(response: Response, videoPath: string, rangeHeader?: string): void {
  const stat = statSync(videoPath)
  const total = stat.size
  response.setHeader('Accept-Ranges', 'bytes')
  response.setHeader('Content-Type', 'video/mp4')
  response.setHeader('Cache-Control', 'no-store')

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
