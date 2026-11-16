import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/userinsight'
const columns = [
  '报告编号',
  '分析主题',
  '目标人群',
  '关键指标',
  '核心发现',
  '数据区间',
  '生成时间',
  '下次生成',
]
const actions = ['提交生成', '发布报告', '归档报告']
const statuses = ['草稿', '生成中', '已发布', '已归档']

type Row = Record<string, string | number | boolean | null>
type Change = {
  指标: string
  基期值: number | null
  本期值: number | null
  变化率: number | null
  变化类型: string
  展示: string
}
type Finding = {
  结论: string
  指标: string
  依据批次: string[]
  数据区间: string
  基线快照: string | null
}
type Snapshot = {
  快照编号: string
  版本: number
  是否当前: boolean
  数据区间: string
  生成时间: string
  生成方式: string
  扫描方式?: string
  基线快照: string | null
  基线区间: string | null
  对比说明?: string
  关键指标变化: Change[]
  核心发现: Finding[]
  区间判定: {
    选定区间: string
    取舍: string
    排除批次: { 批次编号: string; 状态: string; 数据区间: string; 原因: string }[]
    说明: string[]
  }
  扫描?: { 扫描批次: string[]; 复用批次: string[]; 扫描行数: number; 缓存命中: number }
}
type BatchItem = {
  ok: boolean
  报告id: number
  报告编号: string | null
  message: string
  快照编号?: string
  快照版本?: number
  数据区间?: string
  生成方式?: string
  扫描批次?: string[]
  复用批次?: string[]
}
type Trace = {
  当前快照: Snapshot | null
  快照版本: { 快照编号: string; 版本: number; 是否当前: boolean; 数据区间: string; 生成时间: string; 生成方式: string; 基线快照: string | null }[]
  区间判定: Snapshot['区间判定'] | null
  扫描明细: { 批次编号: string; 数据区间: string; 来源: string; 本次扫描行数: number; 缓存命中: boolean }[]
  状态记录: Record<string, string | string[] | number>[]
  订阅交付: { 订阅编号?: string; 快照编号?: string; 交付时间?: string }[]
}

function StatRow({ summary }: { summary: Record<string, string | number> }) {
  const cards = [
    { label: '洞察报告总数', value: summary['报告总数'] ?? 0 },
    { label: '已发布报告', value: summary['已发布报告'] ?? 0 },
    { label: '生成中报告', value: summary['生成中报告'] ?? 0 },
    { label: '待刷新报告', value: summary['待刷新报告'] ?? 0 },
    { label: '当前快照数', value: summary['当前快照数'] ?? 0 },
  ]
  return (
    <div className="stat-row">
      {cards.map((item) => (
        <article key={item.label} className="stat-card">
          <span className="stat-label">{item.label}</span>
          <strong className="stat-value">{String(item.value)}</strong>
        </article>
      ))}
    </div>
  )
}

export default function UserinsightView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [summary, setSummary] = useState<Record<string, string | number>>({})
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [staleOnly, setStaleOnly] = useState(false)
  const [selected, setSelected] = useState<number[]>([])
  const [batchResults, setBatchResults] = useState<BatchItem[] | null>(null)
  const [snapshots, setSnapshots] = useState<{ report: Row; items: Snapshot[] } | null>(null)
  const [activeVersion, setActiveVersion] = useState<number | null>(null)
  const [trace, setTrace] = useState<{ report: Row; data: Trace } | null>(null)
  const [crowds, setCrowds] = useState<Record<string, unknown>[]>([])
  const [crowdEdit, setCrowdEdit] = useState({ reference: '', definition: '' })
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters)
    if (staleOnly) query.set('refresh', 'stale')
    try {
      const [listResponse, summaryResponse, crowdResponse] = await Promise.all([
        request(`${ENDPOINT}?${query.toString()}`),
        request(`${ENDPOINT}/summary`),
        request(`${ENDPOINT}/crowds`),
      ])
      if (!listResponse.ok) throw new Error('洞察报告列表读取失败')
      const payload = await listResponse.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      if (summaryResponse.ok) setSummary(await summaryResponse.json())
      if (crowdResponse.ok) setCrowds(await crowdResponse.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '用户洞察列表读取失败')
    }
  }, [filters, staleOnly])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) throw new Error(payload.message || '用户洞察动作未生效')
      setNotice(payload.message)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '用户洞察操作失败')
    }
  }

  async function refreshOne(row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/refresh`, {
        method: 'POST',
        body: JSON.stringify({ trigger: '增量刷新' }),
      })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || '增量刷新失败')
      setNotice(
        `快照 ${payload.snapshot.快照编号}（v${payload.snapshot.版本}）已生成，区间 ${payload.snapshot.数据区间}；` +
          `新扫描 ${payload.snapshot.扫描.扫描批次.length} 个批次，复用 ${payload.snapshot.扫描.复用批次.length} 个批次`,
      )
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '增量刷新失败')
    }
  }

  async function batchRefresh() {
    setErrorMessage('')
    setNotice('')
    setBatchResults(null)
    if (selected.length === 0) {
      setErrorMessage('请先勾选至少一份洞察报告')
      return
    }
    try {
      const response = await request(`${ENDPOINT}/batch-refresh`, {
        method: 'POST',
        body: JSON.stringify({ report_ids: selected, trigger: '批量生成' }),
      })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || '批量生成失败')
      setBatchResults(payload.items ?? [])
      setNotice(`批量生成完成：成功 ${payload.succeeded} 份，失败 ${payload.failed} 份`)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '批量生成失败')
    }
  }

  async function openSnapshots(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/snapshots`)
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || '快照读取失败')
      setSnapshots({ report: payload.report, items: payload.items ?? [] })
      const current = (payload.items ?? []).find((item: Snapshot) => item.是否当前)
      setActiveVersion(current ? current.版本 : payload.items?.[0]?.版本 ?? null)
      setTrace(null)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '快照读取失败')
    }
  }

  async function openTrace(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/trace`)
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || '追溯信息读取失败')
      setTrace({ report: payload.报告, data: payload })
      setSnapshots(null)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '追溯信息读取失败')
    }
  }

  async function modifyCrowd() {
    setErrorMessage('')
    setNotice('')
    if (!crowdEdit.reference.trim()) {
      setErrorMessage('请填写目标人群编号，例如 CROWD-0001')
      return
    }
    try {
      const response = await request(`${ENDPOINT}/crowds/${encodeURIComponent(crowdEdit.reference.trim())}/modify`, {
        method: 'POST',
        body: JSON.stringify({ values: { 人群定义: crowdEdit.definition } }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) throw new Error(payload.message || '人群修改失败')
      setNotice(payload.message)
      setCrowdEdit({ reference: '', definition: '' })
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '人群修改失败')
    }
  }

  const activeSnapshot = snapshots?.items.find((item) => item.版本 === activeVersion) ?? null

  function toggle(id: number) {
    setSelected((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]))
  }

  return (
    <section className="page" data-module="userinsight">
      <header className="page-head">
        <div>
          <h2>用户洞察管理</h2>
          <p className="page-desc">
            修改人群只标记待刷新，不重算历史；刷新时复用已扫描批次，快照按报告保留版本。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => void batchRefresh()}>
            批量生成（已选 {selected.length}）
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出用户洞察清单
          </button>
        </div>
      </header>

      <StatRow summary={summary} />

      <div className="panel">
        <strong>目标人群与批次</strong>
        <span className="panel-hint">
          断档或未完成的批次不会进入快照；修改人群只升版本并标记受影响报告。
        </span>
        <ul className="crowd-list">
          {crowds.map((crowd) => (
            <li key={String(crowd.人群编号)}>
              <span>
                {String(crowd.人群编号)} · {String(crowd.人群名称)} · v{String(crowd.版本)}
              </span>
              <span className="batch-tags">
                {(crowd.批次 as { 批次编号: string; 状态: string }[]).map((batch) => (
                  <em
                    key={batch.批次编号}
                    className={batch.状态 === '已完成' ? 'tag ok' : 'tag warn'}
                    title={batch.批次编号}
                  >
                    {batch.批次编号.replace('BATCH-', '')}
                  </em>
                ))}
              </span>
            </li>
          ))}
        </ul>
        <div className="filter-bar">
          <label className="filter-item">
            <span>人群编号</span>
            <input
              value={crowdEdit.reference}
              placeholder="CROWD-0001"
              onChange={(event) => setCrowdEdit((prev) => ({ ...prev, reference: event.target.value }))}
            />
          </label>
          <label className="filter-item grow">
            <span>新的人群定义</span>
            <input
              value={crowdEdit.definition}
              placeholder="例如：注册 14 天内且近 5 日活跃的用户"
              onChange={(event) => setCrowdEdit((prev) => ({ ...prev, definition: event.target.value }))}
            />
          </label>
          <button className="btn" type="button" onClick={() => void modifyCrowd()}>
            修改人群定义
          </button>
        </div>
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <label className="filter-item checkbox">
          <input type="checkbox" checked={staleOnly} onChange={(event) => setStaleOnly(event.target.checked)} />
          <span>只看待刷新</span>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => { setFilters({}); setStaleOnly(false) }}>
          重置条件
        </button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            <th className="select-col" />
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>当前快照</th>
            <th>刷新状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              <td className="select-col">
                <input
                  type="checkbox"
                  checked={selected.includes(Number(row.id))}
                  onChange={() => toggle(Number(row.id))}
                />
              </td>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>
                {row.当前快照编号 ? `${row.当前快照编号} / v${row.当前快照版本}` : '—'}
                {row.人群已变更 && <em className="tag warn">人群已变更</em>}
              </td>
              <td>{String(row.刷新状态 || '—')}</td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void refreshOne(row)}>增量刷新</button>
                <button className="link" type="button" onClick={() => void openSnapshots(row)}>快照</button>
                <button className="link" type="button" onClick={() => void openTrace(row)}>追溯</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 4}>暂无用户洞察数据，可先登记洞察报告</td>
            </tr>
          )}
        </tbody>
      </table>

      {batchResults && (
        <div className="panel">
          <strong>批量生成结果（逐条反馈）</strong>
          <table className="data-table">
            <thead>
              <tr><th>报告</th><th>结果</th><th>快照</th><th>数据区间</th><th>扫描 / 复用批次</th></tr>
            </thead>
            <tbody>
              {batchResults.map((item) => (
                <tr key={item.报告id}>
                  <td>{item.报告编号 || item.报告id}</td>
                  <td className={item.ok ? '' : 'error-text'}>{item.message}</td>
                  <td>{item.快照编号 ? `${item.快照编号} / v${item.快照版本}` : '—'}</td>
                  <td>{item.数据区间 || '—'}</td>
                  <td>
                    {item.ok
                      ? `扫描 ${item.扫描批次?.length ?? 0} / 复用 ${item.复用批次?.length ?? 0}`
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {snapshots && (
        <div className="panel">
          <div className="panel-head">
            <strong>{String(snapshots.report.报告编号)} 的数据快照</strong>
            <button className="btn ghost" type="button" onClick={() => setSnapshots(null)}>关闭</button>
          </div>
          <div className="version-tabs">
            {snapshots.items.map((item) => (
              <button
                key={item.快照编号}
                type="button"
                className={`btn ${item.版本 === activeVersion ? 'primary' : ''}`}
                onClick={() => setActiveVersion(item.版本)}
              >
                v{item.版本} {item.是否当前 ? '当前' : '历史'}
              </button>
            ))}
          </div>
          {activeSnapshot && (
            <>
              <p className="panel-hint">
                {activeSnapshot.快照编号} · {activeSnapshot.生成方式} · 区间 {activeSnapshot.数据区间} ·
                生成于 {activeSnapshot.生成时间}
                {activeSnapshot.基线快照 && ` · 基线 ${activeSnapshot.基线快照}（${activeSnapshot.基线区间}）`}
              </p>
              <h4>关键指标变化</h4>
              <table className="data-table">
                <thead><tr><th>指标</th><th>变化类型</th><th>对比</th></tr></thead>
                <tbody>
                  {activeSnapshot.关键指标变化.map((change) => (
                    <tr key={change.指标}>
                      <td>{change.指标}</td>
                      <td>{change.变化类型}</td>
                      <td>{change.展示}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <h4>核心发现</h4>
              <ul className="finding-list">
                {activeSnapshot.核心发现.map((finding, index) => (
                  <li key={`${finding.指标}-${index}`}>
                    <p>{finding.结论}</p>
                    <small>
                      依据批次：{finding.依据批次.join('、') || '—'} · 数据区间：{finding.数据区间}
                      {finding.基线快照 ? ` · 基线快照：${finding.基线快照}` : ''}
                    </small>
                  </li>
                ))}
              </ul>
              <h4>区间判定</h4>
              <p className="panel-hint">
                {activeSnapshot.区间判定.选定区间} · {activeSnapshot.区间判定.取舍}
              </p>
              <ul className="finding-list">
                {activeSnapshot.区间判定.说明.map((note) => <li key={note}>{note}</li>)}
              </ul>
              <table className="data-table">
                <thead><tr><th>批次</th><th>状态</th><th>数据区间</th><th>未纳入原因</th></tr></thead>
                <tbody>
                  {activeSnapshot.区间判定.排除批次.map((batch) => (
                    <tr key={batch.批次编号}>
                      <td>{batch.批次编号}</td>
                      <td>{batch.状态}</td>
                      <td>{batch.数据区间}</td>
                      <td>{batch.原因}</td>
                    </tr>
                  ))}
                  {activeSnapshot.区间判定.排除批次.length === 0 && (
                    <tr><td className="empty-state" colSpan={4}>没有需要排除的批次</td></tr>
                  )}
                </tbody>
              </table>
            </>
          )}
        </div>
      )}

      {trace && (
        <div className="panel">
          <div className="panel-head">
            <strong>{String(trace.report.报告编号)} 的追溯链路</strong>
            <button className="btn ghost" type="button" onClick={() => setTrace(null)}>关闭</button>
          </div>
          <h4>批次扫描明细</h4>
          <table className="data-table">
            <thead><tr><th>批次</th><th>数据区间</th><th>来源</th><th>扫描行数</th><th>缓存命中</th></tr></thead>
            <tbody>
              {trace.data.扫描明细.map((detail) => (
                <tr key={detail.批次编号}>
                  <td>{detail.批次编号}</td>
                  <td>{detail.数据区间}</td>
                  <td>{detail.来源}</td>
                  <td>{detail.本次扫描行数}</td>
                  <td>{detail.缓存命中 ? '是' : '否'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h4>状态记录</h4>
          <ul className="finding-list">
            {trace.data.状态记录.map((log, index) => (
              <li key={index}>
                {String(log.动作)} · {String(log.结果)} · {String(log.时间)} · 快照 {String(log.快照编号)} ·
                区间 {String(log.数据区间)} · 下次生成 {String(log.下次生成)}
              </li>
            ))}
            {trace.data.状态记录.length === 0 && <li>暂无状态记录</li>}
          </ul>
          <h4>快照版本</h4>
          <ul className="finding-list">
            {trace.data.快照版本.map((item) => (
              <li key={item.快照编号}>
                {item.快照编号} · v{item.版本} · {item.是否当前 ? '当前' : '历史'} · {item.数据区间} ·
                {item.生成方式}{item.基线快照 ? ` · 基线 ${item.基线快照}` : ''}
              </li>
            ))}
          </ul>
          <h4>订阅交付</h4>
          <ul className="finding-list">
            {trace.data.订阅交付.map((item, index) => (
              <li key={index}>
                订阅 {String(item.订阅编号)} · 快照 {String(item.快照编号)} · {String(item.交付时间)}
              </li>
            ))}
            {trace.data.订阅交付.length === 0 && <li>当前快照尚未被订阅消费</li>}
          </ul>
        </div>
      )}

      <footer className="page-foot">
        <span>共 {total} 条用户洞察记录{selected.length > 0 ? `，已选 ${selected.length} 条` : ''}</span>
        <span>
          {notice && <span className="notice-text">{notice}</span>}
          {errorMessage && <span className="error-text">{errorMessage}</span>}
        </span>
      </footer>
    </section>
  )
}
