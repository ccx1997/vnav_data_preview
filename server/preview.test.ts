import { describe, expect, it } from 'vitest'
import {
  buildNvencPreviewArguments,
  chooseAvailableGpu,
  parseNvidiaSmiOutput,
  shouldYieldGpu,
  type GpuSnapshot,
} from './preview'

const gpus: GpuSnapshot[] = [
  { index: 0, name: 'busy', gpuUtilization: 88, encoderUtilization: 0, memoryUsedMiB: 1000, memoryTotalMiB: 80000 },
  { index: 1, name: 'free', gpuUtilization: 3, encoderUtilization: 0, memoryUsedMiB: 500, memoryTotalMiB: 80000 },
  { index: 2, name: 'encoding', gpuUtilization: 2, encoderUtilization: 72, memoryUsedMiB: 500, memoryTotalMiB: 80000 },
]

describe('GPU preview selection', () => {
  it('parses nvidia-smi metrics and drops unsupported N/A rows', () => {
    expect(parseNvidiaSmiOutput([
      '0, NVIDIA RTX 4090, 4, 0, 1200, 24564',
      '1, NVIDIA A100, 0, [N/A], 10, 81920',
    ].join('\n'))).toEqual([{
      index: 0,
      name: 'NVIDIA RTX 4090',
      gpuUtilization: 4,
      encoderUtilization: 0,
      memoryUsedMiB: 1200,
      memoryTotalMiB: 24564,
    }])
  })

  it('uses only a capable and idle GPU with enough reserved stream slots', () => {
    const thresholds = {
      maximumGpuUtilization: 30,
      maximumEncoderUtilization: 20,
      minimumFreeMemoryMiB: 1024,
      maximumStreamsPerGpu: 8,
    }
    expect(chooseAvailableGpu(gpus, new Set([0, 1, 2]), new Map(), 6, thresholds)?.index).toBe(1)
    expect(chooseAvailableGpu(gpus, new Set([0, 1, 2]), new Map([[1, 4]]), 6, thresholds)).toBeNull()
    expect(chooseAvailableGpu(gpus, new Set([0, 2]), new Map(), 6, thresholds)).toBeNull()
  })

  it('yields an active preview when a training task takes GPU compute or memory', () => {
    expect(shouldYieldGpu(gpus[0], 70, 512)).toBe(true)
    expect(shouldYieldGpu({ ...gpus[1], memoryUsedMiB: 79_800 }, 70, 512)).toBe(true)
    expect(shouldYieldGpu(gpus[1], 70, 512)).toBe(false)
  })

  it('builds a throttled fragmented MP4 stream without an output file', () => {
    const args = buildNvencPreviewArguments('/data/cam0.mp4', 12.3456, 3)
    expect(args).toContain('-re')
    expect(args).toContain('h264_nvenc')
    expect(args.slice(args.indexOf('-gpu'), args.indexOf('-gpu') + 2)).toEqual(['-gpu', '3'])
    expect(args.slice(args.indexOf('-ss'), args.indexOf('-ss') + 2)).toEqual(['-ss', '12.346'])
    expect(args.at(-1)).toBe('pipe:1')
  })
})
