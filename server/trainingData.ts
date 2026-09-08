import {
  existsSync,
  readFileSync,
  readdirSync,
  statSync,
} from 'node:fs'
import { basename, join, relative, resolve, sep } from 'node:path'
import type {
  TrainingCameraSample,
  TrainingCaseDetail,
  TrainingCasePage,
  TrainingCaseStatus,
  TrainingCommand,
  TrainingRunSummary,
} from '../shared/types.js'

type JsonObject = Record<string, unknown>

interface LoadedRun {
  signature: string
  outputDirectory: string
  summary: TrainingRunSummary
  cases: TrainingCaseDetail[]
  caseByKey: Map<string, TrainingCaseDetail>
}

interface CaseQuery {
  status?: string
  subtask?: string
  query?: string
  offset?: number
  limit?: number
}

interface CameraPosition {
  positionZh: string
  positionEn: string
}

const CAMERA_IDS = ['cam0', 'cam1', 'cam2', 'cam3', 'cam5', 'cam6'] as const
const DEFAULT_CAMERA_POSITIONS: Record<string, CameraPosition> = {
  cam0: { positionZh: '前下', positionEn: 'front_bottom' },
  cam1: { positionZh: '右', positionEn: 'right' },
  cam2: { positionZh: '前上', positionEn: 'front_top' },
  cam3: { positionZh: '左', positionEn: 'left' },
  cam5: { positionZh: '前广', positionEn: 'front_wide' },
  cam6: { positionZh: '后上', positionEn: 'rear_top' },
}
const MEDIA_FILENAMES = new Set([
  ...CAMERA_IDS.map((cameraId) => `${cameraId}.png`),
  'grid.png',
  'fused_grid.png',
])

function asObject(value: unknown): JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonObject
    : {}
}

function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function asBoolean(value: unknown): boolean {
  return value === true
}

function readJson(path: string): JsonObject {
  return asObject(JSON.parse(readFileSync(path, 'utf8')))
}

function readJsonl(path: string): JsonObject[] {
  if (!existsSync(path)) return []
  return readFileSync(path, 'utf8')
    .split('\n')
    .filter((line) => line.trim())
    .map((line) => asObject(JSON.parse(line)))
}

function countLines(path: string): number {
  if (!existsSync(path)) return 0
  return readFileSync(path, 'utf8').split('\n').filter((line) => line.trim()).length
}

function loadCameraPositions(run: JsonObject, documents: JsonObject[]): Record<string, CameraPosition> {
  const positions = { ...DEFAULT_CAMERA_POSITIONS }
  const taskRoot = asString(run.task_root)
  if (!taskRoot) return positions
  const subtasks = [...new Set(documents.map((document) => asString(document.sub_task_id)).filter(Boolean))]
  for (const subtask of subtasks) {
    const taskPath = join(taskRoot, 'meta', 'unpacked', `meta_${subtask}`, 'task.json')
    if (!existsSync(taskPath)) continue
    const sourcePositions = asObject(readJson(taskPath).camera_positions)
    for (const [cameraId, value] of Object.entries(sourcePositions)) {
      const source = asObject(value)
      positions[cameraId] = {
        positionZh: asString(source.position_zh, positions[cameraId]?.positionZh ?? cameraId),
        positionEn: asString(source.position_en, positions[cameraId]?.positionEn ?? cameraId),
      }
    }
    break
  }
  return positions
}

function runId(rootDirectory: string, outputDirectory: string): string {
  return Buffer.from(relative(rootDirectory, outputDirectory)).toString('base64url')
}

function cameraSamples(
  document: JsonObject,
  id: string,
  caseId: string | null,
  cameraPositions: Record<string, CameraPosition>,
): TrainingCameraSample[] {
  const matches = asObject(document.camera_matches)
  return CAMERA_IDS.map((cameraId) => {
    const match = asObject(matches[cameraId])
    return {
      id: cameraId,
      positionZh: cameraPositions[cameraId]?.positionZh ?? cameraId,
      positionEn: cameraPositions[cameraId]?.positionEn ?? cameraId,
      frameIndex: asNumber(match.frame_index, -1),
      deltaMs: asNumber(match.delta_ms),
      positionDeltaMeters: asNumber(match.estimated_position_delta_m),
      yawDeltaDegrees: asNumber(match.estimated_yaw_delta_deg),
      url: caseId
        ? `/training-media/${encodeURIComponent(id)}/cases/${encodeURIComponent(caseId)}/${cameraId}.png`
        : null,
    }
  })
}

function normalizeCase(
  document: JsonObject,
  id: string,
  cameraPositions: Record<string, CameraPosition>,
): TrainingCaseDetail {
  const status: TrainingCaseStatus = document.status === 'accepted' ? 'accepted' : 'rejected'
  const subtaskId = asString(document.sub_task_id)
  const rowIndex = asNumber(document.row_index, -1)
  const caseId = status === 'accepted' ? asString(document.case_id) || null : null
  const key = caseId ?? `rejected-${subtaskId}-${rowIndex}`
  const cameraMatches = asObject(document.camera_matches)
  const syncValues = Object.values(cameraMatches).map((value) => Math.abs(asNumber(asObject(value).delta_ms)))
  const fusion = asObject(document.occupancy_fusion)
  const rollout = asObject(document.rollout)
  const teacher = asObject(document.teacher)
  const diagnostics = asObject(teacher.diagnostics)
  const provenance = asObject(document.provenance)
  const gridPose = asObject(document.grid_pose)
  const projection = asObject(document.route_projection)
  const initialState = asObject(document.initial_state)
  const commands: TrainingCommand[] = Array.isArray(teacher.commands)
    ? teacher.commands.map((value) => {
      const command = asObject(value)
      return {
        linearMps: asNumber(command.linear_mps),
        angularRps: asNumber(command.angular_rps),
        durationSeconds: asNumber(command.duration_s),
      }
    })
    : []
  const staticCropShape = Array.isArray(fusion.static_crop_shape) ? fusion.static_crop_shape : []
  const cropRows = asNumber(staticCropShape[0], 400)
  const cropColumns = asNumber(staticCropShape[1], 400)
  const staticMapPath = asString(provenance.static_map)

  return {
    key,
    caseId,
    status,
    subtaskId,
    rowIndex,
    timestamp: asNumber(document.meta_ts),
    mapName: asString(document.map_name, '—'),
    routeId: asString(document.route_id),
    rejectReason: status === 'rejected' ? asString(document.reject_reason, 'unknown') : null,
    collision: asBoolean(rollout.collision),
    maxSyncMs: syncValues.length ? Math.max(...syncValues) : 0,
    staticAddedCells: asNumber(fusion.static_only_added_obstacle_cells),
    commandCount: commands.length,
    graphName: asString(document.graph_name),
    gridPose: Object.keys(gridPose).length
      ? {
        x: asNumber(gridPose.x),
        y: asNumber(gridPose.y),
        yawRadians: asNumber(gridPose.yaw_rad),
      }
      : null,
    routeProjection: Object.keys(projection).length
      ? {
        distanceMeters: asNumber(projection.distance_m),
        remainingMeters: asNumber(projection.remaining_m),
        progressMeters: asNumber(projection.progress_m),
      }
      : null,
    initialState: Object.keys(initialState).length
      ? {
        linearMps: asNumber(initialState.initial_linear_mps),
        angularRps: asNumber(initialState.initial_angular_rps),
        curvature: asNumber(initialState.initial_curvature),
        indoor: asBoolean(initialState.indoor_profile),
      }
      : null,
    occupancyFusion: Object.keys(fusion).length
      ? {
        version: asString(fusion.version),
        rawObstacleCells: asNumber(fusion.raw_local_obstacle_cells),
        staticObstacleCells: asNumber(fusion.static_obstacle_cells_in_local_extent),
        staticAddedCells: asNumber(fusion.static_only_added_obstacle_cells),
        fusedObstacleCells: asNumber(fusion.fused_local_obstacle_cells),
        sha256: asString(fusion.fused_local_occupancy_sha256),
      }
      : null,
    rollout: Object.keys(rollout).length
      ? {
        collision: asBoolean(rollout.collision),
        collisionStateIndex: typeof rollout.collision_state_index === 'number'
          ? rollout.collision_state_index
          : null,
        durationSeconds: asNumber(rollout.duration_s),
      }
      : null,
    teacher: Object.keys(teacher).length
      ? {
        modelId: asString(teacher.model_id),
        profile: asString(teacher.model_profile),
        inferenceMs: typeof diagnostics.inference_ms === 'number' ? diagnostics.inference_ms : null,
        commands,
      }
      : null,
    cameras: cameraSamples(document, id, caseId, cameraPositions),
    mapRoute: caseId
      ? {
        previewUrl: `/training-media/${encodeURIComponent(id)}/cases/${encodeURIComponent(caseId)}/map-route.png`,
        archiveName: 'inputs.npz',
        sourceMapName: staticMapPath ? basename(staticMapPath) : `${asString(document.graph_name, 'static_map')}.npz`,
        sourceMapSha256: asString(provenance.static_map_sha256),
        cropRows,
        cropColumns,
        resolutionMeters: 0.05,
        extentMeters: cropColumns * 0.05,
        routePointCount: asNumber(diagnostics.path_count),
      }
      : null,
    media: caseId
      ? {
        rawGridUrl: `/training-media/${encodeURIComponent(id)}/cases/${encodeURIComponent(caseId)}/grid.png`,
        fusedGridUrl: `/training-media/${encodeURIComponent(id)}/cases/${encodeURIComponent(caseId)}/fused_grid.png`,
      }
      : null,
  }
}

export class TrainingDataRepository {
  readonly rootDirectory: string
  private readonly cache = new Map<string, LoadedRun>()

  constructor(rootDirectory = process.env.VNAV_TRAINING_ROOT ?? '/mnt/chengchangxu/data/visual_nav_training') {
    this.rootDirectory = resolve(rootDirectory)
  }

  private discoverRunDirectories(): string[] {
    if (!existsSync(this.rootDirectory)) return []
    const output: string[] = []
    for (const task of readdirSync(this.rootDirectory, { withFileTypes: true })) {
      if (!task.isDirectory()) continue
      const taskDirectory = join(this.rootDirectory, task.name)
      for (const pipeline of readdirSync(taskDirectory, { withFileTypes: true })) {
        if (!pipeline.isDirectory()) continue
        const pipelineDirectory = join(taskDirectory, pipeline.name)
        for (const mode of readdirSync(pipelineDirectory, { withFileTypes: true })) {
          if (!mode.isDirectory() || !['pilot', 'full'].includes(mode.name)) continue
          const modeDirectory = join(pipelineDirectory, mode.name)
          for (const run of readdirSync(modeDirectory, { withFileTypes: true })) {
            if (!run.isDirectory() || run.name.startsWith('.')) continue
            const outputDirectory = join(modeDirectory, run.name)
            if (existsSync(join(outputDirectory, 'run.json')) && existsSync(join(outputDirectory, 'teacher_labels.jsonl'))) {
              output.push(outputDirectory)
            }
          }
        }
      }
    }
    return output
  }

  private loadDirectory(outputDirectory: string): LoadedRun {
    const id = runId(this.rootDirectory, outputDirectory)
    const runPath = join(outputDirectory, 'run.json')
    const labelsPath = join(outputDirectory, 'teacher_labels.jsonl')
    const routesPath = join(outputDirectory, 'routes.jsonl')
    const signature = `${statSync(runPath).mtimeMs}:${statSync(labelsPath).mtimeMs}`
    const cached = this.cache.get(id)
    if (cached?.signature === signature) return cached

    const run = readJson(runPath)
    const labelDocuments = readJsonl(labelsPath)
    const cameraPositions = loadCameraPositions(run, labelDocuments)
    const cases = labelDocuments.map((document) => normalizeCase(document, id, cameraPositions))
    const routes = readJsonl(routesPath)
    const acceptedCount = cases.filter((item) => item.status === 'accepted').length
    const rejectedCases = cases.filter((item) => item.status === 'rejected')
    const rejectionReasons: Record<string, number> = {}
    for (const item of rejectedCases) {
      const reason = item.rejectReason ?? 'unknown'
      rejectionReasons[reason] = (rejectionReasons[reason] ?? 0) + 1
    }
    const mode = run.mode === 'pilot' ? 'pilot' : 'full'
    const taskId = asString(run.run_id).split(':')[0] || basename(resolve(outputDirectory, '../../../..'))
    const summary: TrainingRunSummary = {
      id,
      taskId,
      pipelineVersion: asString(run.pipeline_version),
      mode,
      name: basename(outputDirectory),
      status: asString(run.status),
      completedAt: asString(run.completed_at),
      candidateCount: cases.length,
      acceptedCount,
      rejectedCount: rejectedCases.length,
      sourceCount: countLines(join(outputDirectory, 'source_manifest.jsonl')),
      routeCount: routes.length,
      subtaskIds: [...new Set(cases.map((item) => item.subtaskId))],
      rejectionReasons,
      maxSyncMs: cases.length ? Math.max(...cases.map((item) => item.maxSyncMs)) : 0,
      maxRouteLateralMeters: routes.length
        ? Math.max(...routes.map((route) => asNumber(route.max_lateral_error_m)))
        : 0,
      maxRouteTangentDegrees: routes.length
        ? Math.max(...routes.map((route) => asNumber(route.max_tangent_error_deg)))
        : 0,
      fullGenerationAuthorized: asBoolean(run.full_generation_authorized),
    }
    const loaded: LoadedRun = {
      signature,
      outputDirectory,
      summary,
      cases,
      caseByKey: new Map(cases.map((item) => [item.key, item])),
    }
    this.cache.set(id, loaded)
    return loaded
  }

  private findRun(id: string): LoadedRun | null {
    const outputDirectory = this.discoverRunDirectories().find(
      (directory) => runId(this.rootDirectory, directory) === id,
    )
    return outputDirectory ? this.loadDirectory(outputDirectory) : null
  }

  listRuns(): TrainingRunSummary[] {
    return this.discoverRunDirectories()
      .map((directory) => this.loadDirectory(directory).summary)
      .sort((first, second) => second.completedAt.localeCompare(first.completedAt))
  }

  listCases(id: string, query: CaseQuery = {}): TrainingCasePage | null {
    const loaded = this.findRun(id)
    if (!loaded) return null
    const status = query.status === 'accepted' || query.status === 'rejected' ? query.status : 'all'
    const subtask = query.subtask?.trim() ?? ''
    const needle = query.query?.trim().toLowerCase() ?? ''
    const filtered = loaded.cases.filter((item) => {
      if (status !== 'all' && item.status !== status) return false
      if (subtask && item.subtaskId !== subtask) return false
      if (!needle) return true
      return [item.caseId, item.subtaskId, item.rowIndex, item.mapName, item.rejectReason]
        .some((value) => String(value ?? '').toLowerCase().includes(needle))
    })
    const offset = typeof query.offset === 'number' && Number.isFinite(query.offset)
      ? Math.max(0, Math.floor(query.offset))
      : 0
    const limit = typeof query.limit === 'number' && Number.isFinite(query.limit)
      ? Math.min(100, Math.max(1, Math.floor(query.limit)))
      : 30
    return {
      total: filtered.length,
      offset,
      limit,
      items: filtered.slice(offset, offset + limit).map((item) => ({
        key: item.key,
        caseId: item.caseId,
        status: item.status,
        subtaskId: item.subtaskId,
        rowIndex: item.rowIndex,
        timestamp: item.timestamp,
        mapName: item.mapName,
        routeId: item.routeId,
        rejectReason: item.rejectReason,
        collision: item.collision,
        maxSyncMs: item.maxSyncMs,
        staticAddedCells: item.staticAddedCells,
        commandCount: item.commandCount,
      })),
    }
  }

  getCase(id: string, key: string): TrainingCaseDetail | null {
    return this.findRun(id)?.caseByKey.get(key) ?? null
  }

  getMediaPath(id: string, caseId: string, filename: string): string | null {
    if (!MEDIA_FILENAMES.has(filename)) return null
    const loaded = this.findRun(id)
    const item = loaded?.caseByKey.get(caseId)
    if (!loaded || !item || item.caseId !== caseId || item.status !== 'accepted') return null
    const casesRoot = resolve(loaded.outputDirectory, 'cases')
    const path = resolve(casesRoot, caseId, filename)
    if (!path.startsWith(`${casesRoot}${sep}`) || !existsSync(path) || !statSync(path).isFile()) return null
    return path
  }

  getInputArchivePath(id: string, caseId: string): string | null {
    const loaded = this.findRun(id)
    const item = loaded?.caseByKey.get(caseId)
    if (!loaded || !item || item.caseId !== caseId || item.status !== 'accepted') return null
    const casesRoot = resolve(loaded.outputDirectory, 'cases')
    const path = resolve(casesRoot, caseId, 'inputs.npz')
    if (!path.startsWith(`${casesRoot}${sep}`) || !existsSync(path) || !statSync(path).isFile()) return null
    return path
  }
}
