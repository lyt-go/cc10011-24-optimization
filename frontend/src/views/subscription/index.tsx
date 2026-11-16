import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/subscription'
const columns = [
  "订阅编号",
  "订阅内容",
  "接收人",
  "推送渠道",
  "推送频率",
  "数据粒度",
  "静默时段",
  "下次推送时间",
  "关联报告",
  "快照编号"
]
const actions = [
  "提交订阅评审",
  "启用订阅",
  "暂停订阅"
]
const statuses = [
  "草稿",
  "订阅评审中",
  "已启用",
  "已暂停"
]
const stats = [
  { label: "订阅配置总数", value: 0 },
  { label: "已启用订阅", value: 0 },
  { label: "评审中订阅", value: 0 },
  { label: "已消费快照", value: 0 }
]

type Row = Record<string, string | number | null>

type Delivery = {
  交付编号: string
  快照编号: string
  快照版本: number
  数据区间: string
  接收人: string
  推送渠道: string
  状态: string
  交付时间: string
}

export default function SubscriptionView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [notice, setNotice] = useState('')
  const [deliveries, setDeliveries] = useState<{ entry: Row; items: Delivery[] } | null>(null)
  const [summary, setSummary] = useState<Record<string, number>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const [response, summaryResponse] = await Promise.all([
        request(`${ENDPOINT}?${query}`),
        request(`${ENDPOINT}/summary`),
      ])
      if (!response.ok) {
        throw new Error('订阅配置列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      if (summaryResponse.ok) {
        setSummary(await summaryResponse.json())
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '订阅推送列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      if (!response.ok) {
        throw new Error('订阅推送动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '订阅推送操作失败')
    }
  }

  async function consumeSnapshot(row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/consume-insight`, {
        method: 'POST',
        body: JSON.stringify({ values: {} }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message || '消费洞察快照失败')
      }
      setNotice(payload.message)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '消费洞察快照失败')
    }
  }

  async function openDeliveries(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/deliveries`)
      const payload = await response.json()
      if (!response.ok) {
        throw new Error(payload.detail || '交付流水读取失败')
      }
      setDeliveries({ entry: payload.entry, items: payload.items ?? [] })
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '交付流水读取失败')
    }
  }

  return (
    <section className="page" data-module="subscription">
      <header className="page-head">
        <div>
          <h2>订阅推送管理</h2>
          <p className="page-desc">管理数据报告的订阅和推送配置，定时向指定接收人推送分析结果。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('订阅配置登记入口尚未接入审批流')}>
            登记订阅配置
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出订阅推送清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{summary[item.label] ?? item.value}</strong>
          </article>
        ))}
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
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void consumeSnapshot(row)}>
                  消费洞察快照
                </button>
                <button className="link" type="button" onClick={() => void openDeliveries(row)}>交付流水</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无订阅推送数据，可先登记订阅配置</td>
            </tr>
          )}
        </tbody>
      </table>

      {deliveries && (
        <div className="panel">
          <div className="panel-head">
            <strong>{String(deliveries.entry.订阅编号)} 的交付流水</strong>
            <button className="btn ghost" type="button" onClick={() => setDeliveries(null)}>关闭</button>
          </div>
          <table className="data-table">
            <thead>
              <tr><th>交付编号</th><th>快照</th><th>数据区间</th><th>接收人</th><th>渠道</th><th>交付时间</th></tr>
            </thead>
            <tbody>
              {deliveries.items.map((item) => (
                <tr key={item.交付编号}>
                  <td>{item.交付编号}</td>
                  <td>{item.快照编号} / v{item.快照版本}</td>
                  <td>{item.数据区间}</td>
                  <td>{item.接收人}</td>
                  <td>{item.推送渠道}</td>
                  <td>{item.交付时间}</td>
                </tr>
              ))}
              {deliveries.items.length === 0 && (
                <tr><td className="empty-state" colSpan={6}>尚未消费过洞察快照</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      <footer className="page-foot">
        <span>共 {total} 条订阅推送记录</span>
        <span>
          {notice && <span className="notice-text">{notice}</span>}
          {errorMessage && <span className="error-text">{errorMessage}</span>}
        </span>
      </footer>
    </section>
  )
}
