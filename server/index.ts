import express from 'express'
import { readFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { createServer as createViteServer } from 'vite'
import { encodeGridBatch, MAX_GRID_BATCH_FRAMES } from '../shared/gridBatch.js'
import { DatasetRepository, sendVideoWithRange } from './datasets.js'
import { PreviewTranscoder } from './preview.js'
import { renderTrainingMapPreview } from './trainingMapPreview.js'
import { TrainingDataRepository } from './trainingData.js'

const isProduction = process.env.NODE_ENV === 'production'
const useVitePolling = process.env.VNAV_VITE_USE_POLLING === '1'
const port = Number(process.env.PORT ?? 5173)
const host = process.env.HOST ?? '127.0.0.1'
const app = express()
const configuredRoots = process.env.VNAV_DATA_ROOTS
  ?.split(':')
  .map((directory) => directory.trim())
  .filter(Boolean)
const repository = new DatasetRepository(configuredRoots?.length ? configuredRoots : process.env.VNAV_DATA_ROOT)
const trainingRepository = new TrainingDataRepository()
const previewTranscoder = new PreviewTranscoder()

app.disable('x-powered-by')
app.use(express.json({ limit: '16kb' }))

app.get('/api/datasets', (_request, response) => {
  try {
    response.json(repository.list())
  } catch (error) {
    response.status(500).json({ error: error instanceof Error ? error.message : '无法扫描数据目录' })
  }
})

app.get('/api/datasets/:id', (request, response) => {
  try {
    const loaded = repository.load(request.params.id)
    if (!loaded) {
      response.status(404).json({ error: '未找到该数据集' })
      return
    }
    response.json(loaded.detail)
  } catch (error) {
    response.status(500).json({ error: error instanceof Error ? error.message : '无法加载数据集' })
  }
})

app.get('/api/training/runs', (_request, response) => {
  try {
    response.json(trainingRepository.listRuns())
  } catch (error) {
    response.status(500).json({ error: error instanceof Error ? error.message : '无法扫描训练数据目录' })
  }
})

app.get('/api/training/runs/:runId/cases', (request, response) => {
  try {
    const result = trainingRepository.listCases(request.params.runId, {
      status: typeof request.query.status === 'string' ? request.query.status : undefined,
      subtask: typeof request.query.subtask === 'string' ? request.query.subtask : undefined,
      query: typeof request.query.query === 'string' ? request.query.query : undefined,
      offset: Number(request.query.offset),
      limit: Number(request.query.limit),
    })
    if (!result) {
      response.status(404).json({ error: '未找到该训练数据 run' })
      return
    }
    response.json(result)
  } catch (error) {
    response.status(500).json({ error: error instanceof Error ? error.message : '无法读取训练样本列表' })
  }
})

app.get('/api/training/runs/:runId/cases/:caseKey', (request, response) => {
  try {
    const result = trainingRepository.getCase(request.params.runId, request.params.caseKey)
    if (!result) {
      response.status(404).json({ error: '未找到该训练样本' })
      return
    }
    response.json(result)
  } catch (error) {
    response.status(500).json({ error: error instanceof Error ? error.message : '无法读取训练样本' })
  }
})

app.get('/training-media/:runId/cases/:caseId/map-route.png', async (request, response) => {
  try {
    const archivePath = trainingRepository.getInputArchivePath(request.params.runId, request.params.caseId)
    if (!archivePath) {
      response.status(404).json({ error: '未找到该训练样本的地图路线输入' })
      return
    }
    const preview = await renderTrainingMapPreview(archivePath)
    response.setHeader('Cache-Control', 'private, max-age=3600')
    response.type('png').send(preview)
  } catch (error) {
    response.status(500).json({ error: error instanceof Error ? error.message : '无法生成地图路线预览' })
  }
})

app.get('/training-media/:runId/cases/:caseId/:filename', (request, response) => {
  try {
    const path = trainingRepository.getMediaPath(
      request.params.runId,
      request.params.caseId,
      request.params.filename,
    )
    if (!path) {
      response.status(404).json({ error: '未找到该训练样本媒体' })
      return
    }
    response.setHeader('Cache-Control', 'private, max-age=3600')
    response.sendFile(path)
  } catch (error) {
    response.status(500).json({ error: error instanceof Error ? error.message : '无法读取训练样本媒体' })
  }
})

app.delete('/api/datasets/:id', async (request, response) => {
  try {
    const result = await repository.delete(request.params.id)
    if (!result) {
      response.status(404).json({ error: '未找到该数据集' })
      return
    }
    response.json(result)
  } catch (error) {
    response.status(500).json({ error: error instanceof Error ? error.message : '无法删除数据集' })
  }
})

app.post('/api/datasets/:id/trim', async (request, response) => {
  try {
    const trimStart = Number(request.body?.trimStart)
    const trimEnd = Number(request.body?.trimEnd)
    const result = await repository.trim(request.params.id, trimStart, trimEnd)
    if (!result) {
      response.status(404).json({ error: '未找到该数据集' })
      return
    }
    response.json(result)
  } catch (error) {
    const message = error instanceof Error ? error.message : '无法裁剪数据集'
    const status = /必须|请至少|过长|没有可保留/.test(message) ? 400 : /正在处理/.test(message) ? 409 : 500
    response.status(status).json({ error: message })
  }
})

app.post('/api/datasets/:id/map-name', async (request, response) => {
  try {
    const result = await repository.setMapName(request.params.id, request.body?.mapName)
    if (!result) {
      response.status(404).json({ error: '未找到该数据集' })
      return
    }
    response.json(result)
  } catch (error) {
    const message = error instanceof Error ? error.message : '无法写入地图标签'
    const status = /必须是|不是有效 JSON|不是 JSON 对象|没有可标注/.test(message)
      ? 400
      : /正在处理/.test(message)
        ? 409
        : 500
    response.status(status).json({ error: message })
  }
})

app.post('/api/preview/sessions', async (request, response) => {
  const streams = Number(request.body?.streams)
  try {
    response.json(await previewTranscoder.createSession(Number.isFinite(streams) ? streams : 1))
  } catch (error) {
    response.status(500).json({ error: error instanceof Error ? error.message : '无法检测 GPU 预览资源' })
  }
})

app.delete('/api/preview/sessions/:sessionId', (request, response) => {
  previewTranscoder.releaseSession(request.params.sessionId)
  response.status(204).end()
})

app.get('/media/:id/cameras/:camera/preview', (request, response) => {
  try {
    const videoPath = repository.getCameraPath(request.params.id, request.params.camera)
    if (!videoPath) {
      response.status(404).json({ error: '未找到该相机视频' })
      return
    }
    const sessionId = typeof request.query.session === 'string' ? request.query.session : ''
    const start = Number(request.query.start)
    if (!sessionId || !previewTranscoder.stream(sessionId, videoPath, Number.isFinite(start) ? start : 0, response)) {
      response.status(409).json({ error: 'GPU 预览会话已释放，请重新播放' })
    }
  } catch (error) {
    response.status(500).json({ error: error instanceof Error ? error.message : '无法启动 GPU 预览' })
  }
})

app.get('/media/:id/cameras/:camera', (request, response) => {
  try {
    const videoPath = repository.getCameraPath(request.params.id, request.params.camera)
    if (!videoPath) {
      response.status(404).json({ error: '未找到该相机视频' })
      return
    }
    sendVideoWithRange(response, videoPath, request.headers.range)
  } catch (error) {
    response.status(500).json({ error: error instanceof Error ? error.message : '无法读取视频' })
  }
})

app.post('/media/:id/grids/batch', async (request, response) => {
  const requestedFilenames: unknown = request.body?.filenames
  const filenames: string[] = Array.isArray(requestedFilenames)
    ? [...new Set(requestedFilenames.filter((value): value is string => typeof value === 'string'))]
    : []
  if (!filenames.length || filenames.length > MAX_GRID_BATCH_FRAMES) {
    response.status(400).json({ error: `占据图批次必须包含 1–${MAX_GRID_BATCH_FRAMES} 个文件` })
    return
  }
  try {
    const startedAt = performance.now()
    const paths = repository.getGridPaths(request.params.id, filenames)
    if (!paths || paths.some((path) => path === null)) {
      response.status(404).json({ error: '批次中包含不存在的占据图' })
      return
    }
    const contents = await Promise.all(paths.map((path) => readFile(path!)))
    const payload = encodeGridBatch(contents.map((bytes, index) => ({
      filename: filenames[index],
      bytes,
    })))
    response.setHeader('Cache-Control', 'no-store')
    response.setHeader('Content-Type', 'application/vnd.vnav.grid-batch')
    response.setHeader('Content-Length', payload.byteLength)
    response.setHeader('X-Grid-Count', contents.length)
    response.setHeader('Server-Timing', `grid-read;dur=${(performance.now() - startedAt).toFixed(1)}`)
    response.send(Buffer.from(payload.buffer, payload.byteOffset, payload.byteLength))
  } catch (error) {
    response.status(500).json({ error: error instanceof Error ? error.message : '无法批量读取占据图' })
  }
})

app.get('/media/:id/grids/:filename', (request, response) => {
  try {
    const gridPath = repository.getGridPath(request.params.id, request.params.filename)
    if (!gridPath) {
      response.status(404).json({ error: '未找到该占据图' })
      return
    }
    response.setHeader('Cache-Control', 'no-store')
    response.sendFile(gridPath)
  } catch (error) {
    response.status(500).json({ error: error instanceof Error ? error.message : '无法读取占据图' })
  }
})

if (isProduction) {
  const distDirectory = resolve(process.cwd(), 'dist')
  app.use(express.static(distDirectory))
  app.get('*', (_request, response) => response.sendFile(resolve(distDirectory, 'index.html')))
} else {
  const vite = await createViteServer({
    server: {
      middlewareMode: true,
      ...(useVitePolling ? { watch: { usePolling: true, interval: 500 } } : {}),
    },
    appType: 'spa',
  })
  app.use(vite.middlewares)
}

app.listen(port, host, () => {
  console.log(`VNav 数据预览已启动：http://${host}:${port}`)
})
