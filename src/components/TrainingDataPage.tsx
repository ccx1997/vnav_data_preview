import { useEffect, useMemo, useState } from 'react'
import type {
  TrainingCaseDetail,
  TrainingCasePage,
  TrainingCaseStatus,
  TrainingRunSummary,
} from '../../shared/types'
import { arrangeCameras } from '../lib/cameraLayout'

interface TrainingDataPageProps {
  onOpenSource: () => void
}

type StatusFilter = 'all' | TrainingCaseStatus

async function fetchJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(typeof body.error === 'string' ? body.error : `请求失败（${response.status}）`)
  }
  return body as T
}

function formatNumber(value: number, digits = 3): string {
  return Number.isFinite(value) ? value.toFixed(digits) : '—'
}

function formatTimestamp(value: number): string {
  if (!Number.isFinite(value)) return '—'
  return value.toFixed(3)
}

function shortSubtask(value: string): string {
  return value.includes('_') ? `_${value.split('_').at(-1)}` : value
}

export function TrainingDataPage({ onOpenSource }: TrainingDataPageProps) {
  const [runs, setRuns] = useState<TrainingRunSummary[]>([])
  const [runId, setRunId] = useState('')
  const [status, setStatus] = useState<StatusFilter>('accepted')
  const [subtask, setSubtask] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [search, setSearch] = useState('')
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState<TrainingCasePage | null>(null)
  const [selectedKey, setSelectedKey] = useState('')
  const [detail, setDetail] = useState<TrainingCaseDetail | null>(null)
  const [loadingRuns, setLoadingRuns] = useState(true)
  const [loadingCases, setLoadingCases] = useState(false)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [error, setError] = useState('')
  const pageSize = 30

  useEffect(() => {
    const controller = new AbortController()
    setLoadingRuns(true)
    fetchJson<TrainingRunSummary[]>('/api/training/runs', controller.signal)
      .then((items) => {
        setRuns(items)
        setRunId((current) => current || items.find((item) => item.mode === 'full')?.id || items[0]?.id || '')
      })
      .catch((reason: unknown) => {
        if ((reason as Error).name !== 'AbortError') setError((reason as Error).message)
      })
      .finally(() => setLoadingRuns(false))
    return () => controller.abort()
  }, [])

  useEffect(() => {
    if (!runId) {
      setPage(null)
      setSelectedKey('')
      setDetail(null)
      return
    }
    const controller = new AbortController()
    const parameters = new URLSearchParams({
      status,
      offset: String(offset),
      limit: String(pageSize),
    })
    if (subtask) parameters.set('subtask', subtask)
    if (search) parameters.set('query', search)
    setLoadingCases(true)
    setError('')
    fetchJson<TrainingCasePage>(
      `/api/training/runs/${encodeURIComponent(runId)}/cases?${parameters}`,
      controller.signal,
    )
      .then((result) => {
        setPage(result)
        setSelectedKey((current) => result.items.some((item) => item.key === current)
          ? current
          : result.items[0]?.key ?? '')
      })
      .catch((reason: unknown) => {
        if ((reason as Error).name !== 'AbortError') setError((reason as Error).message)
      })
      .finally(() => setLoadingCases(false))
    return () => controller.abort()
  }, [offset, runId, search, status, subtask])

  useEffect(() => {
    if (!runId || !selectedKey) {
      setDetail(null)
      return
    }
    const controller = new AbortController()
    setLoadingDetail(true)
    fetchJson<TrainingCaseDetail>(
      `/api/training/runs/${encodeURIComponent(runId)}/cases/${encodeURIComponent(selectedKey)}`,
      controller.signal,
    )
      .then(setDetail)
      .catch((reason: unknown) => {
        if ((reason as Error).name !== 'AbortError') setError((reason as Error).message)
      })
      .finally(() => setLoadingDetail(false))
    return () => controller.abort()
  }, [runId, selectedKey])

  const activeRun = runs.find((item) => item.id === runId) ?? null
  const pageEnd = page ? Math.min(page.total, page.offset + page.items.length) : 0
  const hasPrevious = (page?.offset ?? 0) > 0
  const hasNext = page ? page.offset + page.items.length < page.total : false
  const rejectedLabel = activeRun
    ? Object.entries(activeRun.rejectionReasons).map(([reason, count]) => `${reason} ${count}`).join(' · ')
    : ''
  const title = detail?.caseId ?? (detail ? `拒绝样本 · row ${detail.rowIndex}` : '选择一个样本')
  const subtaskOptions = useMemo(() => activeRun?.subtaskIds ?? [], [activeRun])

  function changeRun(nextRunId: string) {
    setRunId(nextRunId)
    setOffset(0)
    setSubtask('')
    setSearchInput('')
    setSearch('')
    setSelectedKey('')
  }

  function changeStatus(nextStatus: StatusFilter) {
    setStatus(nextStatus)
    setOffset(0)
    setSelectedKey('')
  }

  return (
    <div className="training-shell">
      <header className="training-topbar">
        <div className="brand">
          <span className="brand-mark"><i /></span>
          <div>
            <span>VNAV</span>
            <strong>数据检查</strong>
          </div>
        </div>
        <nav className="workspace-tabs" aria-label="数据视图">
          <button type="button" onClick={onOpenSource}>采集预览</button>
          <button type="button" className="is-active" aria-current="page">训练数据</button>
        </nav>
        <div className="training-heading">
          <span className="eyebrow">TRAINING DATA REVIEW</span>
          <strong>{activeRun ? `${activeRun.taskId} · ${activeRun.name}` : '等待训练数据'}</strong>
        </div>
        <label className="training-run-select">
          <span>RUN</span>
          <select value={runId} onChange={(event) => changeRun(event.target.value)} disabled={!runs.length}>
            {runs.map((run) => (
              <option key={run.id} value={run.id}>
                {run.taskId} · {run.mode} · {run.name}
              </option>
            ))}
          </select>
        </label>
      </header>

      {loadingRuns ? (
        <main className="training-state">
          <div className="loader" />
          <h1>正在索引训练数据</h1>
          <p>读取 run、Manifest 和标签摘要…</p>
        </main>
      ) : !runs.length ? (
        <main className="training-state">
          <span className="state-symbol state-symbol--empty">0</span>
          <h1>没有找到训练数据 run</h1>
          <p>请检查 VNAV_TRAINING_ROOT 或默认 visual_nav_training 目录。</p>
        </main>
      ) : (
        <>
          <section className="training-metrics" aria-label="全量指标">
            <div><span>候选</span><strong>{activeRun?.candidateCount.toLocaleString() ?? '—'}</strong></div>
            <div><span>接受</span><strong className="metric-good">{activeRun?.acceptedCount.toLocaleString() ?? '—'}</strong></div>
            <div><span>拒绝</span><strong className="metric-warning">{activeRun?.rejectedCount.toLocaleString() ?? '—'}</strong></div>
            <div><span>最大同步</span><strong>{formatNumber(activeRun?.maxSyncMs ?? Number.NaN, 2)}<small> ms</small></strong></div>
            <div><span>路线误差</span><strong>{formatNumber(activeRun?.maxRouteLateralMeters ?? Number.NaN, 3)}<small> m</small></strong></div>
            <div><span>路线切向</span><strong>{formatNumber(activeRun?.maxRouteTangentDegrees ?? Number.NaN, 2)}<small>°</small></strong></div>
          </section>

          <main className="training-workspace">
            <aside className="training-browser">
              <div className="training-filters">
                <div className="status-segments" aria-label="样本状态">
                  {([
                    ['accepted', '接受'],
                    ['rejected', '拒绝'],
                    ['all', '全部'],
                  ] as const).map(([value, label]) => (
                    <button
                      key={value}
                      type="button"
                      className={status === value ? 'is-active' : ''}
                      aria-pressed={status === value}
                      onClick={() => changeStatus(value)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <label>
                  <span>子片段</span>
                  <select value={subtask} onChange={(event) => { setSubtask(event.target.value); setOffset(0) }}>
                    <option value="">全部</option>
                    {subtaskOptions.map((value) => <option key={value} value={value}>{value}</option>)}
                  </select>
                </label>
                <form className="training-search" onSubmit={(event) => {
                  event.preventDefault()
                  setSearch(searchInput.trim())
                  setOffset(0)
                }}>
                  <input
                    value={searchInput}
                    onChange={(event) => setSearchInput(event.target.value)}
                    placeholder="case / row / 原因"
                    aria-label="搜索训练样本"
                  />
                  <button type="submit">查找</button>
                </form>
              </div>

              <div className="case-list-heading">
                <span>{loadingCases ? '正在读取…' : `${page?.total.toLocaleString() ?? 0} 个样本`}</span>
                {status === 'rejected' && rejectedLabel ? <small title={rejectedLabel}>{rejectedLabel}</small> : null}
              </div>
              <div className="training-case-list" aria-label="训练样本列表">
                {page?.items.map((item) => (
                  <button
                    type="button"
                    key={item.key}
                    className={`training-case-row${selectedKey === item.key ? ' is-selected' : ''}`}
                    onClick={() => setSelectedKey(item.key)}
                  >
                    <span className={`case-status case-status--${item.status}`} />
                    <span className="case-row-main">
                      <strong>{item.caseId ?? `row ${item.rowIndex}`}</strong>
                      <small>{shortSubtask(item.subtaskId)} · {item.mapName} · {formatTimestamp(item.timestamp)}</small>
                    </span>
                    <span className="case-row-metric">
                      {item.status === 'accepted' ? `+${item.staticAddedCells.toLocaleString()}` : '拒绝'}
                    </span>
                  </button>
                ))}
                {!loadingCases && page?.items.length === 0 ? (
                  <div className="case-list-empty">当前筛选条件下没有样本</div>
                ) : null}
              </div>
              <div className="training-pagination">
                <button type="button" disabled={!hasPrevious} onClick={() => setOffset(Math.max(0, offset - pageSize))}>上一页</button>
                <span>{page?.total ? `${page.offset + 1}–${pageEnd} / ${page.total}` : '0 / 0'}</span>
                <button type="button" disabled={!hasNext} onClick={() => setOffset(offset + pageSize)}>下一页</button>
              </div>
            </aside>

            <section className="training-detail" aria-live="polite">
              <div className="training-detail-heading">
                <div>
                  <span className="eyebrow">{detail?.status === 'rejected' ? 'REJECTED SAMPLE' : 'MATERIALIZED SAMPLE'}</span>
                  <h1>{title}</h1>
                </div>
                {detail ? (
                  <div className="detail-tags">
                    <span>{shortSubtask(detail.subtaskId)}</span>
                    <span>{detail.mapName}</span>
                    <span>row {detail.rowIndex}</span>
                    <span>sync {formatNumber(detail.maxSyncMs, 1)} ms</span>
                  </div>
                ) : null}
              </div>

              {loadingDetail ? (
                <div className="detail-loading"><div className="loader" />正在读取样本</div>
              ) : detail?.status === 'rejected' ? (
                <div className="rejected-detail">
                  <span className="rejected-symbol">×</span>
                  <div>
                    <h2>{detail.rejectReason}</h2>
                    <p>该候选保留在教师标签 Manifest 中，但未物化六路图片和训练输入。</p>
                    <dl>
                      <div><dt>子片段</dt><dd>{detail.subtaskId}</dd></div>
                      <div><dt>时间戳</dt><dd>{formatTimestamp(detail.timestamp)}</dd></div>
                      <div><dt>碰撞状态</dt><dd>{detail.rollout?.collision ? `state ${detail.rollout.collisionStateIndex}` : '—'}</dd></div>
                      <div><dt>静态补障碍</dt><dd>{detail.staticAddedCells.toLocaleString()} cells</dd></div>
                    </dl>
                  </div>
                </div>
              ) : detail ? (
                <div className="training-detail-scroll">
                  <section className="training-camera-section">
                    <div className="section-heading"><h2>六路相机</h2><span>同一锚点 · 实际 PTS 最近邻</span></div>
                    <div className="training-camera-grid">
                      {arrangeCameras(detail.cameras).map((camera, index) => camera ? (
                        <figure key={camera.id}>
                          {camera.url ? <img src={camera.url} alt={`${camera.positionZh} ${camera.id} 训练帧`} loading="lazy" /> : null}
                          <figcaption>
                            <span className="training-camera-name"><strong>{camera.positionZh}</strong><code>{camera.id}</code></span>
                            <span>frame {camera.frameIndex} · {camera.deltaMs >= 0 ? '+' : ''}{formatNumber(camera.deltaMs, 1)} ms</span>
                          </figcaption>
                        </figure>
                      ) : (
                        <figure className="training-camera-empty" key={`empty-${index}`}>
                          <span>未配置相机</span>
                        </figure>
                      ))}
                    </div>
                  </section>

                  <section className="training-lower-grid">
                    <div className="occupancy-compare">
                      <div className="section-heading"><h2>占据输入</h2><span>黑白 PNG 0/255 · 教师数组 int8 0/100</span></div>
                      <div className="occupancy-pair">
                        <figure>
                          <img src={detail.media?.rawGridUrl} alt="原始局部占据图" />
                          <figcaption>原始局部观测</figcaption>
                        </figure>
                        <figure>
                          <img src={detail.media?.fusedGridUrl} alt="融合后的教师占据图" />
                          <figcaption>融合后黑白预览</figcaption>
                        </figure>
                      </div>
                      <div className="fusion-stats">
                        <span>原始障碍 <strong>{detail.occupancyFusion?.rawObstacleCells.toLocaleString() ?? '—'}</strong></span>
                        <span>静态新增 <strong>+{detail.occupancyFusion?.staticAddedCells.toLocaleString() ?? '—'}</strong></span>
                        <span>融合障碍 <strong>{detail.occupancyFusion?.fusedObstacleCells.toLocaleString() ?? '—'}</strong></span>
                      </div>
                    </div>

                    {detail.mapRoute ? (
                      <div className="map-route-panel">
                        <div className="section-heading">
                          <h2>静态地图 + 路线</h2>
                          <span>车头向上 · {formatNumber(detail.mapRoute.extentMeters, 0)} m × {formatNumber(detail.mapRoute.extentMeters, 0)} m</span>
                        </div>
                        <figure className="map-route-preview">
                          <img src={detail.mapRoute.previewUrl} alt="静态地图、forward route 与教师 rollout" />
                        </figure>
                        <div className="map-route-legend" aria-label="地图图例">
                          <span><i className="legend-obstacle" />静态障碍</span>
                          <span><i className="legend-route" />forward route</span>
                          <span><i className="legend-rollout" />rollout</span>
                          <span><i className="legend-robot" />当前车位</span>
                        </div>
                        <dl className="map-route-storage">
                          <div><dt>来源地图</dt><dd title={detail.mapRoute.sourceMapSha256}>{detail.mapRoute.sourceMapName}</dd></div>
                          <div><dt>map sha256</dt><dd title={detail.mapRoute.sourceMapSha256}>{detail.mapRoute.sourceMapSha256 ? `${detail.mapRoute.sourceMapSha256.slice(0, 12)}…` : '—'}</dd></div>
                          <div><dt>static_occupancy</dt><dd>{detail.mapRoute.cropRows}×{detail.mapRoute.cropColumns} · int8 0/100</dd></div>
                          <div><dt>forward_route_mask</dt><dd>{detail.mapRoute.cropRows}×{detail.mapRoute.cropColumns} · uint8 0/1</dd></div>
                          <div><dt>world_to_pixel</dt><dd>3×3 · float64</dd></div>
                          <div><dt>forward_route</dt><dd>{detail.mapRoute.routePointCount}×2 · world XY</dd></div>
                          <div><dt>权威文件</dt><dd>{detail.mapRoute.archiveName}</dd></div>
                        </dl>
                      </div>
                    ) : null}

                    <div className="teacher-action-panel">
                      <div className="section-heading"><h2>教师动作</h2><span>{detail.teacher?.modelId ?? '—'} · {detail.teacher?.profile || '—'}</span></div>
                      <table>
                        <thead><tr><th>step</th><th>linear</th><th>angular</th><th>duration</th></tr></thead>
                        <tbody>
                          {detail.teacher?.commands.map((command, index) => (
                            <tr key={index}>
                              <td>{index + 1}</td>
                              <td>{formatNumber(command.linearMps)}</td>
                              <td>{formatNumber(command.angularRps)}</td>
                              <td>{formatNumber(command.durationSeconds, 2)} s</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      <dl className="training-metadata">
                        <div><dt>grid pose</dt><dd>{detail.gridPose ? `${formatNumber(detail.gridPose.x)}, ${formatNumber(detail.gridPose.y)}, ${formatNumber(detail.gridPose.yawRadians)} rad` : '—'}</dd></div>
                        <div><dt>route</dt><dd>{detail.routeProjection ? `${formatNumber(detail.routeProjection.distanceMeters)} m / remaining ${formatNumber(detail.routeProjection.remainingMeters, 2)} m` : '—'}</dd></div>
                        <div><dt>initial v / w</dt><dd>{detail.initialState ? `${formatNumber(detail.initialState.linearMps)} / ${formatNumber(detail.initialState.angularRps)}` : '—'}</dd></div>
                        <div><dt>inference</dt><dd>{detail.teacher?.inferenceMs === null ? '—' : `${formatNumber(detail.teacher?.inferenceMs ?? Number.NaN, 2)} ms`}</dd></div>
                      </dl>
                    </div>
                  </section>
                </div>
              ) : (
                <div className="detail-empty">从左侧选择一个训练样本</div>
              )}
              {error ? <div className="training-error" role="alert">{error}</div> : null}
            </section>
          </main>
        </>
      )}
    </div>
  )
}
