import { describe, expect, it } from 'vitest'
import { arrangeCameras } from './cameraLayout'

describe('arrangeCameras', () => {
  it('uses the same rear/front/side slots for source and training previews', () => {
    const cameras = [
      { id: 'cam0', positionZh: '前下' },
      { id: 'cam1', positionZh: '右' },
      { id: 'cam2', positionZh: '前上' },
      { id: 'cam3', positionZh: '左' },
      { id: 'cam5', positionZh: '前广' },
      { id: 'cam6', positionZh: '后上' },
    ]
    expect(arrangeCameras(cameras).map((camera) => camera?.id ?? null)).toEqual([
      'cam6',
      'cam2',
      'cam5',
      'cam3',
      'cam0',
      'cam1',
    ])
  })
})
