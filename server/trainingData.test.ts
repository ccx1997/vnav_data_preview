import { existsSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { TrainingDataRepository } from './trainingData'

const roots: string[] = []

function createFixture(): string {
  const root = join(tmpdir(), `vnav-training-preview-${process.pid}-${Date.now()}`)
  roots.push(root)
  const run = join(root, 'task-a', 'vnav_teacher_v1', 'full', 'run_20260827_010000')
  const caseDirectory = join(run, 'cases', 'sample_0000001')
  mkdirSync(caseDirectory, { recursive: true })
  writeFileSync(join(run, 'run.json'), JSON.stringify({
    run_id: 'task-a:full:20260827_010000',
    mode: 'full',
    status: 'success',
    pipeline_version: 'vnav_teacher_v1',
    completed_at: '2026-08-27T01:10:00+08:00',
    full_generation_authorized: true,
  }))
  writeFileSync(join(run, 'source_manifest.jsonl'), '{}\n{}\n')
  writeFileSync(join(run, 'routes.jsonl'), JSON.stringify({
    max_lateral_error_m: 0.08,
    max_tangent_error_deg: 4.2,
  }))
  const base = {
    sub_task_id: 'task-a_1',
    map_name: 'P_map',
    graph_name: 'map-a',
    route_id: 'task-a_1:route_000',
    grid_pose: { x: 1, y: 2, yaw_rad: 0.3 },
    route_projection: { distance_m: 0.02, remaining_m: 9, progress_m: 3 },
    initial_state: {
      initial_linear_mps: 0.2,
      initial_angular_rps: 0.1,
      initial_curvature: 0.5,
      indoor_profile: false,
    },
    occupancy_fusion: {
      version: 'fusion-v1',
      raw_local_obstacle_cells: 10,
      static_obstacle_cells_in_local_extent: 20,
      static_only_added_obstacle_cells: 7,
      fused_local_obstacle_cells: 17,
      fused_local_occupancy_sha256: 'hash',
    },
    camera_matches: {
      cam0: {
        frame_index: 3,
        delta_ms: -12,
        estimated_position_delta_m: 0.01,
        estimated_yaw_delta_deg: 0.2,
      },
    },
    teacher: {
      model_id: 'teacher.pt',
      model_profile: 'outdoor',
      diagnostics: { inference_ms: 13.4 },
      commands: [{ linear_mps: 0.5, angular_rps: 0.1, duration_s: 0.3 }],
    },
  }
  writeFileSync(join(run, 'teacher_labels.jsonl'), [
    JSON.stringify({
      ...base,
      status: 'accepted',
      case_id: 'sample_0000001',
      row_index: 8,
      meta_ts: 100.5,
      reject_reason: null,
      rollout: { collision: false, collision_state_index: null, duration_s: 1.5 },
    }),
    JSON.stringify({
      ...base,
      status: 'rejected',
      case_id: null,
      row_index: 9,
      meta_ts: 100.7,
      reject_reason: 'teacher_rollout_collision',
      rollout: { collision: true, collision_state_index: 4, duration_s: 1.5 },
    }),
  ].join('\n'))
  for (const filename of ['cam0.png', 'grid.png', 'fused_grid.png', 'inputs.npz']) {
    writeFileSync(join(caseDirectory, filename), filename)
  }
  return root
}

afterEach(() => {
  while (roots.length) rmSync(roots.pop()!, { recursive: true, force: true })
})

describe('TrainingDataRepository', () => {
  it('discovers runs and summarizes accepted and rejected labels', () => {
    const repository = new TrainingDataRepository(createFixture())
    const runs = repository.listRuns()
    expect(runs).toHaveLength(1)
    expect(runs[0]).toMatchObject({
      taskId: 'task-a',
      mode: 'full',
      candidateCount: 2,
      acceptedCount: 1,
      rejectedCount: 1,
      sourceCount: 2,
      routeCount: 1,
      rejectionReasons: { teacher_rollout_collision: 1 },
      maxSyncMs: 12,
      maxRouteLateralMeters: 0.08,
    })
  })

  it('paginates and filters cases while retaining rejected diagnostics', () => {
    const repository = new TrainingDataRepository(createFixture())
    const run = repository.listRuns()[0]
    expect(repository.listCases(run.id, { status: 'accepted' })).toMatchObject({
      total: 1,
      items: [{ caseId: 'sample_0000001', status: 'accepted' }],
    })
    const rejected = repository.listCases(run.id, { status: 'rejected', query: 'collision' })
    expect(rejected?.items[0]).toMatchObject({
      caseId: null,
      status: 'rejected',
      rejectReason: 'teacher_rollout_collision',
      collision: true,
    })
    expect(repository.getCase(run.id, rejected!.items[0].key)?.rollout).toMatchObject({
      collision: true,
      collisionStateIndex: 4,
    })
    expect(repository.getCase(run.id, 'sample_0000001')?.cameras[0]).toMatchObject({
      id: 'cam0',
      positionZh: '前下',
      positionEn: 'front_bottom',
    })
    expect(repository.getCase(run.id, 'sample_0000001')?.mapRoute).toMatchObject({
      archiveName: 'inputs.npz',
      cropRows: 400,
      cropColumns: 400,
      extentMeters: 20,
    })
  })

  it('serves only allowlisted media belonging to a materialized case', () => {
    const repository = new TrainingDataRepository(createFixture())
    const run = repository.listRuns()[0]
    const image = repository.getMediaPath(run.id, 'sample_0000001', 'fused_grid.png')
    expect(image && existsSync(image)).toBe(true)
    expect(repository.getMediaPath(run.id, 'sample_0000001', '../run.json')).toBeNull()
    expect(repository.getMediaPath(run.id, 'rejected-task-a_1-9', 'grid.png')).toBeNull()
    expect(repository.getMediaPath('../bad', 'sample_0000001', 'grid.png')).toBeNull()
    expect(repository.getInputArchivePath(run.id, 'sample_0000001')).toMatch(/inputs\.npz$/)
    expect(repository.getInputArchivePath(run.id, '../sample_0000001')).toBeNull()
  })
})
