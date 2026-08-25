import express from 'express'
import { resolve } from 'node:path'
import { createServer as createViteServer } from 'vite'
import { DatasetRepository, sendVideoWithRange } from './datasets.js'

const isProduction = process.env.NODE_ENV === 'production'
const port = Number(process.env.PORT ?? 5173)
const host = process.env.HOST ?? '127.0.0.1'
const app = express()
const configuredRoots = process.env.VNAV_DATA_ROOTS
  ?.split(':')
  .map((directory) => directory.trim())
  .filter(Boolean)
const repository = new DatasetRepository(configuredRoots?.length ? configuredRoots : process.env.VNAV_DATA_ROOT)

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
    server: { middlewareMode: true },
    appType: 'spa',
  })
  app.use(vite.middlewares)
}

app.listen(port, host, () => {
  console.log(`VNav 数据预览已启动：http://${host}:${port}`)
})
