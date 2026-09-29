import { asBoolean, asNumber, asObject, asString } from './datasetFrames.js'
import type { JsonObject, RawFrameLine } from './datasetFrames.js'

interface TrimDatasetIdentity {
  id: string
  grouped: boolean
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

export function updateTaskMetadata(
  task: JsonObject,
  descriptor: TrimDatasetIdentity,
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

export function updateExportMetadata(
  exportMeta: JsonObject,
  descriptor: TrimDatasetIdentity,
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

export function updateVideoMetadata(
  metadata: JsonObject,
  descriptor: TrimDatasetIdentity,
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
