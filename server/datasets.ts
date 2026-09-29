import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { rename, rm, stat, unlink, writeFile } from 'node:fs/promises'
import { basename, dirname, join, resolve } from 'node:path'
import type {
  CameraInfo,
  DatasetDeleteResult,
  DatasetDetail,
  DatasetMapNameResult,
  DatasetSummary,
  DatasetTrimResult,
} from '../shared/types.js'
import { MAP_NAMES } from '../shared/types.js'
import {
  asBoolean,
  asMapName,
  asNumber,
  asObject,
  asString,
  asStringArray,
  parseFramesJsonl,
  parseRawFrameLines,
} from './datasetFrames.js'
import type { JsonObject } from './datasetFrames.js'
import { updateExportMetadata, updateTaskMetadata, updateVideoMetadata } from './datasetTrimMetadata.js'

function readJson(path: string): JsonObject {
  return asObject(JSON.parse(readFileSync(path, 'utf8')))
}

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
  descriptor: DatasetDescriptor
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
      mapName: parsed.mapName,
      frameCount: parsed.frames.length,
      complete: summary.complete && parseWarnings.length === 0,
      warningCount: summary.warningCount + parseWarnings.length,
      timeline: { from, to, duration: Math.max(0, to - from) },
      cameras,
      frames: parsed.frames,
      parseWarnings,
    }
    const loaded = { signature, descriptor, detail, videos, grids: parsed.grids }
    this.cache.set(id, loaded)
    return loaded
  }

  getCameraPath(id: string, cameraId: string): string | null {
    return this.load(id)?.videos.get(cameraId) ?? null
  }

  getGridPath(id: string, filename: string): string | null {
    return this.getGridPaths(id, [filename])?.[0] ?? null
  }

  getGridPaths(id: string, filenames: string[]): Array<string | null> | null {
    const loaded = this.load(id)
    if (!loaded) return null
    return filenames.map((filename) => {
      if (basename(filename) !== filename || !/^\d+\.png$/.test(filename) || !loaded.grids.has(filename)) return null
      const path = join(loaded.descriptor.gridsDirectory, filename)
      return existsSync(path) ? path : null
    })
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

  async setMapName(id: string, mapNameValue: unknown): Promise<DatasetMapNameResult | null> {
    const descriptor = this.getDescriptor(id)
    if (!descriptor) return null
    const mapName = asMapName(mapNameValue)
    if (!mapName) throw new Error(`地图名称必须是 ${MAP_NAMES.join('、')} 之一`)
    if (this.activeOperations.has(id)) throw new Error('该数据集正在处理，请稍后再试')

    this.activeOperations.add(id)
    const temporary = `${descriptor.framesPath}.map-name-${randomUUID()}`
    try {
      const content = readFileSync(descriptor.framesPath, 'utf8')
      let labeledFrames = 0
      const updatedLines = content.split(/\r?\n/).map((line, index) => {
        if (!line.trim()) return line
        let value: unknown
        try {
          value = JSON.parse(line)
        } catch {
          throw new Error(`frames.jsonl 第 ${index + 1} 行不是有效 JSON，未写入地图标签`)
        }
        if (value === null || typeof value !== 'object' || Array.isArray(value)) {
          throw new Error(`frames.jsonl 第 ${index + 1} 行不是 JSON 对象，未写入地图标签`)
        }
        labeledFrames += 1
        return JSON.stringify({ ...(value as JsonObject), map_name: mapName })
      })
      if (!labeledFrames) throw new Error('frames.jsonl 没有可标注的数据行')

      const sourceStat = await stat(descriptor.framesPath)
      await writeFile(temporary, updatedLines.join('\n'), { mode: sourceStat.mode })
      await rename(temporary, descriptor.framesPath)
      this.cache.delete(id)
      return { id, mapName, labeledFrames }
    } finally {
      await rm(temporary, { force: true }).catch(() => undefined)
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

export { parseFramesJsonl } from './datasetFrames.js'
export { sendVideoWithRange, resolveByteRange } from './videoRange.js'
