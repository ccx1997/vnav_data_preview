import { render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { CameraInfo } from '../../shared/types'
import { CameraWall } from './CameraWall'

describe('CameraWall', () => {
  it('sorts the screenshot cameras by their displayed Chinese position labels', () => {
    const positions = {
      cam0: { positionZh: '前下', positionEn: 'front_bottom' },
      cam1: { positionZh: '右', positionEn: 'right' },
      cam2: { positionZh: '前上', positionEn: 'front_top' },
      cam3: { positionZh: '左', positionEn: 'left' },
      cam5: { positionZh: '前广', positionEn: 'front_wide' },
      cam6: { positionZh: '后', positionEn: 'rear' },
    }
    const cameras: CameraInfo[] = Object.entries(positions).map(([id, position]) => ({
      id,
      ...position,
      videoUrl: `/media/example/cameras/${id}`,
      available: true,
      coverage: null,
      coverageWarning: '',
    }))

    const { container } = render(
      <CameraWall cameras={cameras} registerVideo={vi.fn()} onMediaError={vi.fn()} />,
    )

    expect(
      [...container.querySelectorAll('.camera-tile')].map(
        (tile) => tile.querySelector('video')?.dataset.camera ?? null,
      ),
    ).toEqual(['cam6', 'cam2', 'cam5', 'cam3', 'cam0', 'cam1'])
    expect(
      [...container.querySelectorAll('.camera-tile')].map(
        (tile) => tile.querySelector('.camera-label span')?.textContent ?? null,
      ),
    ).toEqual(['后', '前上', '前广', '左', '前下', '右'])
    expect(container.querySelectorAll('video')).toHaveLength(6)
  })

  it('recognizes the Chinese position aliases used by grouped datasets', () => {
    const positions = {
      cam0: '左',
      cam1: '右',
      cam2: '前下',
      cam3: '前上',
      cam5: '右中',
      cam6: '后左',
    }
    const cameras: CameraInfo[] = Object.entries(positions).map(([id, positionZh]) => ({
      id,
      positionZh,
      positionEn: '',
      videoUrl: `/media/example/cameras/${id}`,
      available: true,
      coverage: null,
      coverageWarning: '',
    }))

    const { container } = render(
      <CameraWall cameras={cameras} registerVideo={vi.fn()} onMediaError={vi.fn()} />,
    )

    expect(
      [...container.querySelectorAll('.camera-label span')].map((label) => label.textContent),
    ).toEqual(['后左', '前上', '右中', '左', '前下', '右'])
  })
})
