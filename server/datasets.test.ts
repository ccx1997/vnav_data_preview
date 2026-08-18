import { mkdirSync, rmSync, writeFileSync } from 'node:fs'
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
