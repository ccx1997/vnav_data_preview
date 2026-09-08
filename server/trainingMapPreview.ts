import { spawn } from 'node:child_process'
import { statSync } from 'node:fs'
import { resolve } from 'node:path'

const MAX_PREVIEW_BYTES = 4 * 1024 * 1024
const MAX_CACHE_ENTRIES = 128
const cache = new Map<string, Promise<Buffer>>()

function spawnRenderer(archivePath: string): Promise<Buffer> {
  const scriptPath = resolve(process.cwd(), 'server', 'renderTrainingMap.py')
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(process.env.VNAV_PYTHON ?? 'python3', [scriptPath, archivePath], {
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    const output: Buffer[] = []
    const errors: Buffer[] = []
    let outputBytes = 0

    child.stdout.on('data', (chunk: Buffer) => {
      outputBytes += chunk.length
      if (outputBytes > MAX_PREVIEW_BYTES) {
        child.kill()
        return
      }
      output.push(chunk)
    })
    child.stderr.on('data', (chunk: Buffer) => {
      if (errors.reduce((sum, item) => sum + item.length, 0) < 16_384) errors.push(chunk)
    })
    child.on('error', rejectPromise)
    child.on('close', (code) => {
      if (outputBytes > MAX_PREVIEW_BYTES) {
        rejectPromise(new Error('地图路线预览超过大小限制'))
        return
      }
      const bytes = Buffer.concat(output)
      if (code !== 0 || bytes.length < 8 || bytes.subarray(1, 4).toString('ascii') !== 'PNG') {
        const detail = Buffer.concat(errors).toString('utf8').trim()
        rejectPromise(new Error(detail || `地图路线预览生成失败（exit ${code ?? 'unknown'}）`))
        return
      }
      resolvePromise(bytes)
    })
  })
}

export function renderTrainingMapPreview(archivePath: string): Promise<Buffer> {
  const stat = statSync(archivePath)
  const key = `${archivePath}:${stat.mtimeMs}:${stat.size}`
  const cached = cache.get(key)
  if (cached) return cached
  const rendering = spawnRenderer(archivePath).catch((error) => {
    cache.delete(key)
    throw error
  })
  cache.set(key, rendering)
  while (cache.size > MAX_CACHE_ENTRIES) cache.delete(cache.keys().next().value!)
  return rendering
}
