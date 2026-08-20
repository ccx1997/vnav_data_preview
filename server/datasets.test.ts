import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { DatasetRepository, parseFramesJsonl, resolveByteRange } from './datasets'

const roots: string[] = []

function createFixture(): string {
  const root = join(tmpdir(), `vnav-preview-${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}`)
  roots.push(root)
  const meta = join(root, 'meta_example.1')
  const videos = join(root, 'videos_example.1')
  mkdirSync(join(meta, 'grids'), { recursive: true })
  mkdirSync(videos, { recursive: true })
  writeFileSync(
    join(meta, 'task.json'),
    JSON.stringify({
      task_id: 'at-example',
      title: '测试数据',
      robot_id: 'Robot-1',
      started_ts: 100,
      ended_ts: 110,
      started_at_local: '2026-01-01 10:00:00',
      camera_positions: { cam0: { position_zh: '左', position_en: 'left' } },
    }),
  )
  writeFileSync(join(meta, 'export_meta.json'), JSON.stringify({ sample_count: 3 }))
  writeFileSync(
    join(videos, 'manifest.json'),
    JSON.stringify({
      from: 100,
      to: 110,
      cameras: ['cam0'],
      partial: false,
      per_camera: { cam0: { file: 'cam0.mp4', coverage: 1 } },
    }),
  )
  writeFileSync(join(videos, 'cam0.mp4'), 'video')
  writeFileSync(join(meta, 'grids', '100000.png'), 'png')
  writeFileSync(
    join(meta, 'frames.jsonl'),
    [
      JSON.stringify({
        ts: 100,
        ts_ms: 100000,
        datetime_local: '2026-01-01 10:00:00.000',
        pose: { x: 1, y: 2, yaw: 3, valid: true },
        actual_vel: { vx: 0.1, vy: 0, wz: 0.2, valid: true, src: 'odom' },
        grid_valid: true,
        grid_png: 'grids/100000.png',
        width: 200,
        height: 200,
        resolution: 0.05,
        frame_id: 'body',
      }),
      '{broken',
      JSON.stringify({ pose: { valid: false } }),
    ].join('\n'),
  )
  return root
}

function createGroupedFixture(): string {
  const root = join(tmpdir(), `vnav-preview-grouped-${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}`)
  roots.push(root)
  const id = 'batch_2'
  const meta = join(root, 'meta_batch_all', `meta_${id}`)
  const videos = join(root, 'videos_batch_all', id)
  mkdirSync(join(meta, 'grids'), { recursive: true })
  mkdirSync(videos, { recursive: true })
  writeFileSync(
    join(meta, 'task.json'),
    JSON.stringify({
      task_id: 'batch',
      title: '批量测试',
      robot_id: 'Robot-2',
      started_ts: 100,
      ended_ts: 500,
      started_at_local: '2026-01-01 10:00:00',
      video_cameras: ['cam5'],
      camera_positions: { cam5: { position_zh: '右中', position_en: 'right_mid' } },
      sub_task: { index: 2, sub_task_id: id, from: 200, to: 230 },
    }),
  )
  writeFileSync(
    join(meta, 'export_meta.json'),
    JSON.stringify({ sample_count: 1, sub_task: { index: 2, sub_task_id: id, from: 200, to: 230 } }),
  )
  writeFileSync(
    join(meta, 'video_segments.json'),
    JSON.stringify({ task_id: 'batch', sub_task_id: id, video_cameras: ['cam5'] }),
  )
  writeFileSync(join(videos, 'cam5_continuous.mp4'), 'video')
  writeFileSync(join(meta, 'grids', '200000.png'), 'png')
  writeFileSync(
    join(meta, 'frames.jsonl'),
    JSON.stringify({
      ts: 200,
      ts_ms: 200000,
      pose: { valid: true },
      actual_vel: { valid: true },
      grid_valid: true,
      grid_png: 'grids/200000.png',
    }),
  )
  return root
}

function createTrimFixture(): string {
  const root = join(tmpdir(), `vnav-preview-trim-${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}`)
  roots.push(root)
  const meta = join(root, 'meta_trim-example')
  const videos = join(root, 'videos_trim-example')
  mkdirSync(join(meta, 'grids'), { recursive: true })
  mkdirSync(videos, { recursive: true })
  writeFileSync(
    join(meta, 'task.json'),
    JSON.stringify({
      task_id: 'trim-example',
      title: '裁剪测试',
      robot_id: 'Robot-3',
      started_ts: 100,
      ended_ts: 110,
      tz: 'Asia/Shanghai',
      video_cameras: ['cam0'],
      camera_positions: { cam0: { position_zh: '左', position_en: 'left' } },
    }),
  )
  writeFileSync(
    join(meta, 'export_meta.json'),
    JSON.stringify({ sample_count: 4, grids_total: 4, grids_valid: 4, pose_count: 4, vel_count: 4 }),
  )
  writeFileSync(
    join(videos, 'manifest.json'),
    JSON.stringify({
      from: 100,
      to: 110,
      cameras: ['cam0'],
      partial: false,
      per_camera: { cam0: { file: 'cam0.mp4', output_window: { from: 100, to: 110 }, output_bytes: 5 } },
    }),
  )
  writeFileSync(join(videos, 'cam0.mp4'), 'video')
  const frames = [100, 103, 107, 110].map((timestamp) => ({
    ts: timestamp,
    ts_ms: timestamp * 1000,
    pose: { valid: true },
    actual_vel: { valid: true },
    grid_valid: true,
    grid_png: `grids/${timestamp * 1000}.png`,
  }))
  for (const frame of frames) writeFileSync(join(meta, 'grids', `${frame.ts_ms}.png`), 'png')
  writeFileSync(join(meta, 'frames.jsonl'), frames.map((frame) => JSON.stringify(frame)).join('\n'))
  return root
}

afterEach(() => {
  while (roots.length) rmSync(roots.pop()!, { recursive: true, force: true })
})

describe('parseFramesJsonl', () => {
  it('sorts valid frames and reports malformed or timestamp-less rows', () => {
    const parsed = parseFramesJsonl([
      JSON.stringify({ ts: 2, pose: {}, actual_vel: {} }),
      '{bad',
      JSON.stringify({ ts: 1, pose: {}, actual_vel: {} }),
      JSON.stringify({ pose: {} }),
    ].join('\n'))
    expect(parsed.frames.map((frame) => frame.timestamp)).toEqual([1, 2])
    expect(parsed.warnings).toHaveLength(2)
  })
})

describe('DatasetRepository', () => {
  it('pairs matching folders, loads normalized frames and protects media paths', () => {
    const repository = new DatasetRepository(createFixture())
    const summaries = repository.list()
    expect(summaries).toHaveLength(1)
    expect(summaries[0]).toMatchObject({ id: 'example.1', title: '测试数据', frameCount: 3, complete: true })

    const loaded = repository.load('example.1')
    expect(loaded?.detail.frames).toHaveLength(1)
    expect(loaded?.detail.warningCount).toBe(2)
    expect(loaded?.detail.complete).toBe(false)
    expect(loaded?.detail.cameras[0]).toMatchObject({ id: 'cam0', positionZh: '左', available: true })
    expect(repository.getCameraPath('example.1', 'cam0')).toMatch(/cam0\.mp4$/)
    expect(repository.getGridPath('example.1', '100000.png')).toMatch(/100000\.png$/)
    expect(repository.getGridPath('example.1', '../100000.png')).toBeNull()
    expect(repository.getCameraPath('../example.1', 'cam0')).toBeNull()
  })

  it('discovers datasets nested inside matching batch folders and loads continuous videos', () => {
    const repository = new DatasetRepository(createGroupedFixture())
    const summaries = repository.list()

    expect(summaries).toHaveLength(1)
    expect(summaries[0]).toMatchObject({
      id: 'batch_2',
      taskId: 'batch_2',
      title: '批量测试 · 片段 2',
      startedTimestamp: 200,
      endedTimestamp: 230,
      cameraIds: ['cam5'],
      complete: true,
    })

    const loaded = repository.load('batch_2')
    expect(loaded?.detail.timeline).toEqual({ from: 200, to: 230, duration: 30 })
    expect(loaded?.detail.cameras[0]).toMatchObject({ id: 'cam5', positionZh: '右中', available: true })
    expect(repository.getCameraPath('batch_2', 'cam5')).toMatch(/cam5_continuous\.mp4$/)
    expect(repository.getGridPath('batch_2', '200000.png')).toMatch(/200000\.png$/)
    expect(repository.load('batch_all')).toBeNull()
  })

  it('permanently deletes both flat Meta and video directories', async () => {
    const root = createFixture()
    const repository = new DatasetRepository(root)

    await expect(repository.delete('example.1')).resolves.toEqual({ deletedId: 'example.1' })
    expect(existsSync(join(root, 'meta_example.1'))).toBe(false)
    expect(existsSync(join(root, 'videos_example.1'))).toBe(false)
    expect(repository.list()).toEqual([])
    await expect(repository.delete('example.1')).resolves.toBeNull()
  })

  it('deletes only the selected child from a grouped batch', async () => {
    const root = createGroupedFixture()
    const repository = new DatasetRepository(root)

    await repository.delete('batch_2')
    expect(existsSync(join(root, 'meta_batch_all'))).toBe(true)
    expect(existsSync(join(root, 'videos_batch_all'))).toBe(true)
    expect(existsSync(join(root, 'meta_batch_all', 'meta_batch_2'))).toBe(false)
    expect(existsSync(join(root, 'videos_batch_all', 'batch_2'))).toBe(false)
  })

  it('trims videos, frame Meta, occupancy images and time metadata together', async () => {
    const root = createTrimFixture()
    const trimCalls: Array<{ start: number; duration: number }> = []
    const repository = new DatasetRepository(root, {
      trimVideo: async (_inputPath, outputPath, start, duration) => {
        trimCalls.push({ start, duration })
        writeFileSync(outputPath, 'trimmed-video')
      },
    })

    await expect(repository.trim('trim-example', 2, 2)).resolves.toMatchObject({
      id: 'trim-example',
      from: 102,
      to: 108,
      duration: 6,
      removedFrames: 2,
      removedGrids: 2,
      trimmedVideos: 1,
    })
    expect(trimCalls).toEqual([{ start: 2, duration: 6 }])
    expect(readFileSync(join(root, 'videos_trim-example', 'cam0.mp4'), 'utf8')).toBe('trimmed-video')
    expect(readFileSync(join(root, 'meta_trim-example', 'frames.jsonl'), 'utf8'))
      .toContain('"ts":103')
    expect(readFileSync(join(root, 'meta_trim-example', 'frames.jsonl'), 'utf8'))
      .not.toContain('"ts":100')
    expect(existsSync(join(root, 'meta_trim-example', 'grids', '100000.png'))).toBe(false)
    expect(existsSync(join(root, 'meta_trim-example', 'grids', '110000.png'))).toBe(false)
    expect(existsSync(join(root, 'meta_trim-example', 'grids', '103000.png'))).toBe(true)
    expect(JSON.parse(readFileSync(join(root, 'videos_trim-example', 'manifest.json'), 'utf8'))).toMatchObject({
      from: 102,
      to: 108,
      per_camera: { cam0: { output_window: { from: 102, to: 108 }, window_s: 6 } },
    })
    expect(JSON.parse(readFileSync(join(root, 'meta_trim-example', 'export_meta.json'), 'utf8'))).toMatchObject({
      sample_count: 2,
      grids_total: 2,
      pose_count: 2,
      vel_count: 2,
    })
    expect(repository.load('trim-example')?.detail).toMatchObject({
      frameCount: 2,
      timeline: { from: 102, to: 108, duration: 6 },
    })
  })

  it('rejects a trim that would remove the entire dataset before changing files', async () => {
    const root = createTrimFixture()
    const repository = new DatasetRepository(root, { trimVideo: async () => undefined })

    await expect(repository.trim('trim-example', 5, 5)).rejects.toThrow('至少需要保留 0.1 秒')
    expect(readFileSync(join(root, 'videos_trim-example', 'cam0.mp4'), 'utf8')).toBe('video')
    expect(readFileSync(join(root, 'meta_trim-example', 'frames.jsonl'), 'utf8')).toContain('"ts":100')
  })
})

describe('resolveByteRange', () => {
  it('supports open, bounded and suffix ranges while rejecting invalid input', () => {
    expect(resolveByteRange('bytes=0-1023', 5000)).toEqual({ start: 0, end: 1023 })
    expect(resolveByteRange('bytes=4000-', 5000)).toEqual({ start: 4000, end: 4999 })
    expect(resolveByteRange('bytes=-500', 5000)).toEqual({ start: 4500, end: 4999 })
    expect(resolveByteRange('bytes=6000-', 5000)).toBeNull()
    expect(resolveByteRange('bytes=0-1,5-6', 5000)).toBeNull()
  })
})
