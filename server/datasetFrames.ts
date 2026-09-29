import { basename } from 'node:path'
import type { FrameSample, GridFrame, MapName, Pose, Velocity } from '../shared/types.js'
import { MAP_NAMES } from '../shared/types.js'

export type JsonObject = Record<string, unknown>

export interface RawFrameLine {
  line: string
  value: JsonObject
  timestamp: number
  gridFilename: string | null
}

export function parseRawFrameLines(content: string): { valid: RawFrameLine[]; passthrough: string[] } {
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

export function asObject(value: unknown): JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as JsonObject)
    : {}
}

export function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

export function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export function asBoolean(value: unknown, fallback = false): boolean {
  return typeof value === 'boolean' ? value : fallback
}

export function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

export function asMapName(value: unknown): MapName | null {
  return typeof value === 'string' && (MAP_NAMES as readonly string[]).includes(value)
    ? value as MapName
    : null
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
  mapName: MapName | null
} {
  const frames: FrameSample[] = []
  const warnings: string[] = []
  const grids = new Set<string>()
  const mapNames = new Set<MapName>()
  let unlabeledFrames = 0

  for (const [index, line] of content.split(/\r?\n/).entries()) {
    if (!line.trim()) continue
    try {
      const frame = asObject(JSON.parse(line))
      const mapName = asMapName(frame.map_name)
      if (mapName) mapNames.add(mapName)
      else unlabeledFrames += 1
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
  const mapName = mapNames.size === 1 && unlabeledFrames === 0 ? [...mapNames][0] : null
  return { frames, warnings, grids, mapName }
}
