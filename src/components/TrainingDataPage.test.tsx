import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  TrainingCaseDetail,
  TrainingCasePage,
  TrainingRunSummary,
} from '../../shared/types'
import { TrainingDataPage } from './TrainingDataPage'

const run: TrainingRunSummary = {
  id: 'run-a',
  taskId: '20260820180215WDK',
  pipelineVersion: 'vnav_teacher_v1',
  mode: 'full',
  name: 'run_20260827_012110',
  status: 'success',
  completedAt: '2026-08-27T01:34:04+08:00',
  candidateCount: 2555,
  acceptedCount: 2543,
  rejectedCount: 12,
  sourceCount: 5280,
  routeCount: 5,
  subtaskIds: ['20260820180215WDK_1', '20260820180215WDK_5'],
  rejectionReasons: { teacher_rollout_collision: 12 },
  maxSyncMs: 33.332,
  maxRouteLateralMeters: 0.097398,
  maxRouteTangentDegrees: 4.767978,
  fullGenerationAuthorized: true,
}

const accepted: TrainingCaseDetail = {
  key: 'sample_0000001',
  caseId: 'sample_0000001',
  status: 'accepted',
  subtaskId: '20260820180215WDK_1',
  rowIndex: 1,
  timestamp: 1787220139.361,
  mapName: 'P_map',
  routeId: 'route-1',
  rejectReason: null,
  collision: false,
  maxSyncMs: 25.469,
  staticAddedCells: 6963,
  commandCount: 5,
  graphName: 'dufu_community_map',
  gridPose: { x: 4.89, y: 0.713, yawRadians: -0.177 },
  routeProjection: { distanceMeters: 0.00005, remainingMeters: 73.71, progressMeters: 0 },
  initialState: { linearMps: 0.646, angularRps: 0, curvature: 0, indoor: false },
  occupancyFusion: {
    version: 'local_static_obstacle_union_v1',
    rawObstacleCells: 2940,
    staticObstacleCells: 8701,
    staticAddedCells: 6963,
    fusedObstacleCells: 9903,
    sha256: 'fusion-hash',
  },
  rollout: { collision: false, collisionStateIndex: null, durationSeconds: 1 },
  teacher: {
    modelId: 'epoch_01.pt',
    profile: 'outdoor',
    inferenceMs: 23.384,
    commands: Array.from({ length: 5 }, (_, index) => ({
      linearMps: 0.75,
      angularRps: -0.05 + index * 0.001,
      durationSeconds: 0.2,
    })),
  },
  cameras: Object.entries({
    cam0: ['前下', 'front_bottom'],
    cam1: ['右', 'right'],
    cam2: ['前上', 'front_top'],
    cam3: ['左', 'left'],
    cam5: ['前广', 'front_wide'],
    cam6: ['后上', 'rear_top'],
  }).map(([id, [positionZh, positionEn]]) => ({
    id, positionZh, positionEn,
    frameIndex: 10,
    deltaMs: -25.469,
    positionDeltaMeters: 0.018,
    yawDeltaDegrees: 0.165,
    url: `/training-media/run-a/cases/sample_0000001/${id}.png`,
  })),
  mapRoute: {
    previewUrl: '/training-media/run-a/cases/sample_0000001/map-route.png',
    archiveName: 'inputs.npz',
    sourceMapName: 'dufu_community_map.npz',
    sourceMapSha256: 'map-hash',
    cropRows: 400,
    cropColumns: 400,
    resolutionMeters: 0.05,
    extentMeters: 20,
    routePointCount: 156,
  },
  media: {
    rawGridUrl: '/training-media/run-a/cases/sample_0000001/grid.png',
    fusedGridUrl: '/training-media/run-a/cases/sample_0000001/fused_grid.png',
  },
}

const rejected: TrainingCaseDetail = {
  ...accepted,
  key: 'rejected-20260820180215WDK_5-415',
  caseId: null,
  status: 'rejected',
  subtaskId: '20260820180215WDK_5',
  rowIndex: 415,
  rejectReason: 'teacher_rollout_collision',
  collision: true,
  rollout: { collision: true, collisionStateIndex: 21, durationSeconds: 1.5 },
  cameras: accepted.cameras.map((camera) => ({ ...camera, url: null })),
  mapRoute: null,
  media: null,
}

function pageFor(item: TrainingCaseDetail): TrainingCasePage {
  return { total: item.status === 'accepted' ? 2543 : 12, offset: 0, limit: 30, items: [item] }
}

describe('TrainingDataPage', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const body = url === '/api/training/runs'
        ? [run]
        : url.includes('/cases/rejected-')
          ? rejected
          : url.endsWith('/cases/sample_0000001')
            ? accepted
            : url.includes('status=rejected')
              ? pageFor(rejected)
              : pageFor(accepted)
      return { ok: true, status: 200, json: async () => body } as Response
    }))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('shows the materialized camera, binary occupancy and teacher-label evidence', async () => {
    render(<TrainingDataPage onOpenSource={() => {}} />)

    expect(await screen.findByRole('heading', { name: 'sample_0000001' })).toBeInTheDocument()
    expect(screen.getByText('2,555')).toBeInTheDocument()
    expect(screen.getByText('2,543')).toBeInTheDocument()
    expect(screen.getAllByRole('img', { name: /训练帧/ }).map((image) => image.getAttribute('alt'))).toEqual([
      '后上 cam6 训练帧',
      '前上 cam2 训练帧',
      '前广 cam5 训练帧',
      '左 cam3 训练帧',
      '前下 cam0 训练帧',
      '右 cam1 训练帧',
    ])
    expect(screen.getByRole('img', { name: '原始局部占据图' })).toHaveAttribute('src', accepted.media?.rawGridUrl)
    expect(screen.getByRole('img', { name: '融合后的教师占据图' })).toHaveAttribute('src', accepted.media?.fusedGridUrl)
    expect(screen.getByText('黑白 PNG 0/255 · 教师数组 int8 0/100')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: '静态地图、forward route 与教师 rollout' })).toHaveAttribute(
      'src',
      accepted.mapRoute?.previewUrl,
    )
    expect(screen.getByText('400×400 · int8 0/100')).toBeInTheDocument()
    expect(screen.getByText('156×2 · world XY')).toBeInTheDocument()
    expect(screen.getByText('map-hash…')).toBeInTheDocument()
    expect(screen.getAllByRole('row')).toHaveLength(6)
  })

  it('switches to retained rejection diagnostics without inventing media', async () => {
    const onOpenSource = vi.fn()
    render(<TrainingDataPage onOpenSource={onOpenSource} />)
    await screen.findByRole('heading', { name: 'sample_0000001' })

    fireEvent.click(screen.getByRole('button', { name: '拒绝' }))
    expect(await screen.findByRole('heading', { name: 'teacher_rollout_collision' })).toBeInTheDocument()
    expect(screen.getByText(/未物化六路图片/)).toBeInTheDocument()
    expect(screen.getByText('state 21')).toBeInTheDocument()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '采集预览' }))
    await waitFor(() => expect(onOpenSource).toHaveBeenCalledOnce())
  })
})
