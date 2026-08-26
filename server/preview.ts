import { execFile, spawn, type ChildProcess } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { promisify } from 'node:util'
import type { Response } from 'express'
import type { PreviewSessionResult } from '../shared/types.js'

const execFileAsync = promisify(execFile)

export interface GpuSnapshot {
  index: number
  name: string
  gpuUtilization: number
  encoderUtilization: number
  memoryUsedMiB: number
  memoryTotalMiB: number
}

export interface PreviewThresholds {
  maximumGpuUtilization: number
  maximumEncoderUtilization: number
  minimumFreeMemoryMiB: number
  maximumStreamsPerGpu: number
}

interface PreviewSession {
  id: string
  gpuIndex: number
  streamLimit: number
  activeStreams: number
  children: Set<ChildProcess>
  releaseTimer: NodeJS.Timeout | null
}

const DEFAULT_THRESHOLDS: PreviewThresholds = {
  maximumGpuUtilization: 30,
  maximumEncoderUtilization: 20,
  minimumFreeMemoryMiB: 1024,
  maximumStreamsPerGpu: 8,
}

function finiteEnvironmentNumber(name: string, fallback: number): number {
  const value = Number(process.env[name])
  return Number.isFinite(value) && value >= 0 ? value : fallback
}

function readThresholds(): PreviewThresholds {
  return {
    maximumGpuUtilization: finiteEnvironmentNumber(
      'VNAV_PREVIEW_MAX_GPU_UTILIZATION',
      DEFAULT_THRESHOLDS.maximumGpuUtilization,
    ),
    maximumEncoderUtilization: finiteEnvironmentNumber(
      'VNAV_PREVIEW_MAX_ENCODER_UTILIZATION',
      DEFAULT_THRESHOLDS.maximumEncoderUtilization,
    ),
    minimumFreeMemoryMiB: finiteEnvironmentNumber(
      'VNAV_PREVIEW_MIN_FREE_MEMORY_MIB',
      DEFAULT_THRESHOLDS.minimumFreeMemoryMiB,
    ),
    maximumStreamsPerGpu: finiteEnvironmentNumber(
      'VNAV_PREVIEW_MAX_STREAMS_PER_GPU',
      DEFAULT_THRESHOLDS.maximumStreamsPerGpu,
    ),
  }
}

function parseMetric(value: string): number | null {
  const parsed = Number(value.trim())
  return Number.isFinite(parsed) ? parsed : null
}

export function parseNvidiaSmiOutput(output: string): GpuSnapshot[] {
  return output
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .flatMap((line) => {
      const fields = line.split(',').map((field) => field.trim())
      if (fields.length < 6) return []
      const index = parseMetric(fields[0])
      const gpuUtilization = parseMetric(fields.at(-4) ?? '')
      const encoderUtilization = parseMetric(fields.at(-3) ?? '')
      const memoryUsedMiB = parseMetric(fields.at(-2) ?? '')
      const memoryTotalMiB = parseMetric(fields.at(-1) ?? '')
      if (
        index === null || gpuUtilization === null || encoderUtilization === null ||
        memoryUsedMiB === null || memoryTotalMiB === null
      ) return []
      return [{
        index,
        name: fields.slice(1, -4).join(', '),
        gpuUtilization,
        encoderUtilization,
        memoryUsedMiB,
        memoryTotalMiB,
      }]
    })
}

export function chooseAvailableGpu(
  snapshots: GpuSnapshot[],
  capableGpuIndices: Set<number>,
  reservedStreams: Map<number, number>,
  requestedStreams: number,
  thresholds: PreviewThresholds = DEFAULT_THRESHOLDS,
): GpuSnapshot | null {
  return snapshots
    .filter((gpu) => capableGpuIndices.has(gpu.index))
    .filter((gpu) => gpu.gpuUtilization <= thresholds.maximumGpuUtilization)
    .filter((gpu) => gpu.encoderUtilization <= thresholds.maximumEncoderUtilization)
    .filter((gpu) => gpu.memoryTotalMiB - gpu.memoryUsedMiB >= thresholds.minimumFreeMemoryMiB)
    .filter((gpu) => (reservedStreams.get(gpu.index) ?? 0) + requestedStreams <= thresholds.maximumStreamsPerGpu)
    .sort((left, right) => {
      const utilizationDifference = left.gpuUtilization - right.gpuUtilization
      if (utilizationDifference) return utilizationDifference
      const encoderDifference = left.encoderUtilization - right.encoderUtilization
      if (encoderDifference) return encoderDifference
      return left.memoryUsedMiB - right.memoryUsedMiB
    })[0] ?? null
}

export function shouldYieldGpu(gpu: GpuSnapshot, maximumUtilization: number, minimumFreeMemoryMiB: number): boolean {
  return gpu.gpuUtilization >= maximumUtilization || gpu.memoryTotalMiB - gpu.memoryUsedMiB < minimumFreeMemoryMiB
}

export function buildNvencPreviewArguments(inputPath: string, startSeconds: number, gpuIndex: number): string[] {
  const fps = Math.max(1, Math.round(finiteEnvironmentNumber('VNAV_PREVIEW_FPS', 10)))
  const width = Math.max(160, Math.round(finiteEnvironmentNumber('VNAV_PREVIEW_WIDTH', 480)))
  const bitrateKbps = Math.max(100, Math.round(finiteEnvironmentNumber('VNAV_PREVIEW_BITRATE_KBPS', 250)))
  return [
    '-hide_banner', '-loglevel', 'error', '-nostdin',
    '-ss', Math.max(0, startSeconds).toFixed(3),
    '-re', '-i', inputPath,
    '-map', '0:v:0', '-an',
    '-vf', `fps=${fps},scale=w='min(${width},iw)':h=-2`,
    '-c:v', 'h264_nvenc', '-gpu', String(gpuIndex),
    '-preset', 'p4', '-tune', 'll',
    '-b:v', `${bitrateKbps}k`, '-maxrate', `${Math.round(bitrateKbps * 1.35)}k`,
    '-bufsize', `${bitrateKbps * 2}k`,
    '-g', String(fps), '-keyint_min', String(fps), '-forced-idr', '1',
    '-pix_fmt', 'yuv420p',
    '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
    '-frag_duration', '1000000', '-f', 'mp4', 'pipe:1',
  ]
}

export class PreviewTranscoder {
  private readonly sessions = new Map<string, PreviewSession>()
  private readonly probeResults = new Map<number, boolean>()
  private readonly modelProbeResults = new Map<string, boolean>()
  private readonly thresholds = readThresholds()
  private monitorTimer: NodeJS.Timeout | null = null
  private monitorPending = false

  async createSession(requestedStreams: number): Promise<PreviewSessionResult> {
    const streamCount = Math.min(8, Math.max(1, Math.round(requestedStreams)))
    const snapshots = await this.readGpuSnapshots()
    if (!snapshots.length) return this.originalResult('未检测到可查询的 NVIDIA GPU')

    const reserved = new Map<number, number>()
    for (const session of this.sessions.values()) {
      reserved.set(session.gpuIndex, (reserved.get(session.gpuIndex) ?? 0) + session.streamLimit)
    }
    const resourceCandidates = snapshots.filter((gpu) => chooseAvailableGpu(
      [gpu], new Set([gpu.index]), reserved, streamCount, this.thresholds,
    ))
    if (!resourceCandidates.length) return this.originalResult('GPU 正忙或资源不足，使用原始视频')

    const capable = new Set<number>()
    for (const snapshot of resourceCandidates.sort((left, right) => left.gpuUtilization - right.gpuUtilization)) {
      if (await this.supportsNvenc(snapshot)) capable.add(snapshot.index)
    }
    if (!capable.size) return this.originalResult('GPU 不支持 NVENC，使用原始视频')

    const selected = chooseAvailableGpu(snapshots, capable, reserved, streamCount, this.thresholds)
    if (!selected) return this.originalResult('GPU 正忙或资源不足，使用原始视频')

    const id = randomUUID()
    const session: PreviewSession = {
      id,
      gpuIndex: selected.index,
      streamLimit: streamCount,
      activeStreams: 0,
      children: new Set(),
      releaseTimer: null,
    }
    session.releaseTimer = setTimeout(() => this.releaseSession(id), 15_000)
    session.releaseTimer.unref()
    this.sessions.set(id, session)
    this.ensureMonitor()
    return {
      enabled: true,
      mode: 'nvenc',
      sessionId: id,
      gpuIndex: selected.index,
      reason: `GPU ${selected.index} 空闲，已启用实时低码率预览`,
      profile: {
        width: Math.max(160, Math.round(finiteEnvironmentNumber('VNAV_PREVIEW_WIDTH', 480))),
        fps: Math.max(1, Math.round(finiteEnvironmentNumber('VNAV_PREVIEW_FPS', 10))),
        bitrateKbps: Math.max(100, Math.round(finiteEnvironmentNumber('VNAV_PREVIEW_BITRATE_KBPS', 250))),
      },
    }
  }

  stream(sessionId: string, inputPath: string, startSeconds: number, response: Response): boolean {
    const session = this.sessions.get(sessionId)
    if (!session || session.activeStreams >= session.streamLimit * 2) return false
    if (session.releaseTimer) {
      clearTimeout(session.releaseTimer)
      session.releaseTimer = null
    }

    const ffmpeg = process.env.FFMPEG_PATH ?? 'ffmpeg'
    const child = spawn(ffmpeg, buildNvencPreviewArguments(inputPath, startSeconds, session.gpuIndex), {
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    session.activeStreams += 1
    session.children.add(child)
    let stderr = ''
    let finished = false

    response.status(200)
    response.setHeader('Content-Type', 'video/mp4')
    response.setHeader('Cache-Control', 'no-store')
    response.setHeader('X-VNav-Preview-Mode', 'nvenc')
    response.setHeader('X-Accel-Buffering', 'no')

    child.stdout.pipe(response)
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', (chunk: string) => {
      stderr = `${stderr}${chunk}`.slice(-2000)
    })

    const finish = () => {
      if (finished) return
      finished = true
      session.children.delete(child)
      session.activeStreams = Math.max(0, session.activeStreams - 1)
      if (!session.activeStreams && this.sessions.has(session.id)) {
        session.releaseTimer = setTimeout(() => this.releaseSession(session.id), 15_000)
        session.releaseTimer.unref()
      }
    }

    response.on('close', () => {
      if (child.exitCode === null) child.kill('SIGTERM')
      finish()
    })
    child.on('error', () => {
      if (!response.headersSent) response.status(503)
      if (!response.writableEnded) response.end()
      finish()
    })
    child.on('close', (code) => {
      if (code && stderr) console.warn(`GPU 预览流退出（ffmpeg ${code}）：${stderr.trim()}`)
      if (!response.writableEnded) response.end()
      finish()
    })
    return true
  }

  releaseSession(sessionId: string): boolean {
    const session = this.sessions.get(sessionId)
    if (!session) return false
    this.sessions.delete(sessionId)
    if (session.releaseTimer) clearTimeout(session.releaseTimer)
    for (const child of session.children) {
      if (child.exitCode === null) child.kill('SIGTERM')
    }
    session.children.clear()
    if (!this.sessions.size && this.monitorTimer) {
      clearInterval(this.monitorTimer)
      this.monitorTimer = null
    }
    return true
  }

  private originalResult(reason: string): PreviewSessionResult {
    return {
      enabled: false,
      mode: 'original',
      sessionId: null,
      gpuIndex: null,
      reason,
      profile: null,
    }
  }

  private async readGpuSnapshots(): Promise<GpuSnapshot[]> {
    try {
      const { stdout } = await execFileAsync(process.env.NVIDIA_SMI_PATH ?? 'nvidia-smi', [
        '--query-gpu=index,name,utilization.gpu,utilization.encoder,memory.used,memory.total',
        '--format=csv,noheader,nounits',
      ], { timeout: 4000 })
      return parseNvidiaSmiOutput(stdout)
    } catch {
      return []
    }
  }

  private async supportsNvenc(gpu: GpuSnapshot): Promise<boolean> {
    const cached = this.probeResults.get(gpu.index) ?? this.modelProbeResults.get(gpu.name)
    if (cached !== undefined) return cached
    try {
      await execFileAsync(process.env.FFMPEG_PATH ?? 'ffmpeg', [
        '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-f', 'lavfi', '-i', 'color=size=64x64:rate=1',
        '-frames:v', '1', '-c:v', 'h264_nvenc', '-gpu', String(gpu.index),
        '-f', 'null', '-',
      ], { timeout: 6000, maxBuffer: 64 * 1024 })
      this.probeResults.set(gpu.index, true)
      this.modelProbeResults.set(gpu.name, true)
      return true
    } catch (error) {
      const detail = String((error as { stderr?: string }).stderr ?? error)
      if (/unsupported device|no capable devices/i.test(detail)) {
        this.probeResults.set(gpu.index, false)
        this.modelProbeResults.set(gpu.name, false)
      }
      return false
    }
  }

  private ensureMonitor(): void {
    if (this.monitorTimer) return
    this.monitorTimer = setInterval(() => void this.monitorSessions(), 10_000)
    this.monitorTimer.unref()
  }

  private async monitorSessions(): Promise<void> {
    if (this.monitorPending || !this.sessions.size) return
    this.monitorPending = true
    try {
      const snapshots = await this.readGpuSnapshots()
      const maximumUtilization = finiteEnvironmentNumber('VNAV_PREVIEW_STOP_GPU_UTILIZATION', 70)
      const minimumFreeMemoryMiB = finiteEnvironmentNumber('VNAV_PREVIEW_STOP_MIN_FREE_MEMORY_MIB', 512)
      for (const session of [...this.sessions.values()]) {
        const gpu = snapshots.find((snapshot) => snapshot.index === session.gpuIndex)
        if (!gpu || shouldYieldGpu(gpu, maximumUtilization, minimumFreeMemoryMiB)) {
          console.warn(`GPU ${session.gpuIndex} 资源被其他任务占用，停止实时预览并回退原始视频`)
          this.releaseSession(session.id)
        }
      }
    } finally {
      this.monitorPending = false
    }
  }
}
