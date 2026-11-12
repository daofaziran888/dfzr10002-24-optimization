import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/report'
const columns = [
  "报告编号",
  "报告标题",
  "关联数据集",
  "分析周期",
  "报告状态",
  "业务日期",
  "终态时间",
  "执行代次",
  "通知结果"
]
const actions = [
  "生成报告",
  "归档报告",
  "删除报告"
]
const statuses = [
  "草稿",
  "生成中",
  "已生成",
  "已归档"
]
const stats = [
  {
    "label": "分析报告总数",
    "value": 0
  },
  {
    "label": "已生成报告",
    "value": 0
  },
  {
    "label": "已归档报告",
    "value": 0
  }
]

type NotificationRow = {
  id: number
  渠道: string
  状态: string
  尝试次数: number
  发送时间: string | null
  取消时间: string | null
  失败原因: string | null
  执行代次?: number
  重试记录: { attempt: number; result: string; at: string }[]
}

type Execution = {
  id: number
  执行代次: number
  业务日期: string
  开始时间: string
  完成时间: string | null
  终态时间: string | null
  执行状态: string
  报告状态: string
  通知结果: string
  notifications: NotificationRow[]
}

type Row = Record<string, string | number | boolean | null> & {
  id: number
  notifications?: NotificationRow[]
  notification_history?: NotificationRow[]
  execution?: Execution | null
}

function resultTone(result: string) {
  if (result === '已发送') return 'tone-ok'
  if (result === '部分失败' || result === '部分取消' || result === '待重试') return 'tone-warn'
  if (result === '发送失败' || result === '已取消') return 'tone-bad'
  return 'tone-muted'
}

export default function ReportView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [detail, setDetail] = useState<Row | null>(null)
  const filterFields = useMemo(() => [
    { label: '报告编号', query: 'keyword' },
    { label: '状态', query: 'status' },
  ], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams()
    for (const field of filterFields) {
      const value = filters[field.label]?.trim()
      if (value) query.set(field.query, value)
    }
    const queryText = query.toString()
    try {
      const response = await request(`${ENDPOINT}${queryText ? `?${queryText}` : ''}`)
      if (!response.ok) {
        throw new Error('分析报告列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析报告管理列表读取失败')
    }
  }, [filters, filterFields])

  useEffect(() => {
    void reload()
  }, [reload])

  async function openDetail(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}`)
      if (!response.ok) {
        throw new Error('分析报告详情读取失败')
      }
      setDetail(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析报告详情读取失败')
    }
  }

  async function runAction(action: string, row: Row, extra?: Record<string, string>) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action, ...extra }),
      })
      const payload = await response.json().catch(() => null)
      if (!response.ok || !payload?.ok) {
        throw new Error(payload?.message || '分析报告管理动作未生效，请稍后重试')
      }
      setNotice(payload.message)
      await reload()
      if (detail?.id === row.id) {
        await openDetail(row)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析报告管理操作失败')
    }
  }

  const notifications = detail?.notifications ?? []
  const historyNotifications = detail?.notification_history ?? []

  return (
    <section className="page" data-module="report">
      <header className="page-head">
        <div>
          <h2>分析报告管理</h2>
          <p className="page-desc">
            报告进入终态后才发送通知；业务日期沿用定时任务执行开始时间，跨午夜不切到次日。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('分析报告登记入口尚未接入审批流')}>
            登记分析报告
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出分析报告管理清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field.label} className="filter-item">
            <span>{field.label}</span>
            <input
              value={filters[field.label] ?? ''}
              placeholder={field.label === '状态' ? '如：已生成' : `按${field.label}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field.label]: event.target.value }))}
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
            <tr key={String(row.id)} className={detail?.id === row.id ? 'row-active' : undefined}>
              {columns.map((column) => {
                if (column === '报告编号') {
                  return (
                    <td key={column}>
                      <button className="link" type="button" onClick={() => void openDetail(row)}>
                        {String(row[column] ?? '—')}
                      </button>
                    </td>
                  )
                }
                if (column === '通知结果' || column === '报告状态') {
                  const value = String(row[column] ?? '—')
                  return <td key={column}><span className={`badge ${resultTone(value)}`}>{value}</span></td>
                }
                return <td key={column}>{String(row[column] ?? '—')}</td>
              })}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无分析报告管理数据，可先登记分析报告</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条分析报告管理记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      {detail && (
        <aside className="detail-panel">
          <header className="detail-head">
            <div>
              <h3>{String(detail["报告编号"])} · {String(detail["报告标题"])}</h3>
              <p className="page-desc">
                业务日期：{String(detail["业务日期"])}，终态时间：{String(detail["终态时间"])}，
                执行代次：{String(detail["执行代次"])}
              </p>
            </div>
            <button className="btn ghost" type="button" onClick={() => setDetail(null)}>关闭</button>
          </header>

          <div className="execution-meta">
            <span>报告状态 <span className={`badge ${resultTone(String(detail["报告状态"]))}`}>{String(detail["报告状态"])}</span></span>
            <span>通知结果 <span className={`badge ${resultTone(String(detail["通知结果"]))}`}>{String(detail["通知结果"])}</span></span>
            <span>开始 {String(detail["开始时间"] ?? '—')}</span>
            <span>终态 {String(detail["终态时间"] ?? '—')}</span>
            <span>任务 {String(detail["schedule_task_no"] ?? '—')}</span>
          </div>

          {!detail["报告终态"] && (
            <p className="hint-text">报告尚未进入终态，通知保持待发送，不会提前发出成功或失败提示。</p>
          )}

          <table className="data-table inner-table">
            <thead>
              <tr><th>通知渠道</th><th>状态</th><th>尝试</th><th>发送时间</th><th>取消时间</th><th>说明</th><th>操作</th></tr>
            </thead>
            <tbody>
              {notifications.map((item) => (
                <tr key={item.id}>
                  <td>{item.渠道}</td>
                  <td><span className={`badge ${resultTone(item.状态)}`}>{item.状态}</span></td>
                  <td>{item.尝试次数}</td>
                  <td>{item.发送时间 ?? '—'}</td>
                  <td>{item.取消时间 ?? '—'}</td>
                  <td>{item.失败原因 ?? '—'}</td>
                  <td className="row-actions">
                    {(item.状态 === '待重试' || item.状态 === '发送失败') && (
                      <button
                        className="link"
                        type="button"
                        onClick={() => void runAction('重试通知', detail, { channel: item.渠道 })}
                      >
                        重试通知
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {notifications.length === 0 && (
                <tr><td className="empty-state" colSpan={7}>当前执行代次暂无通知记录</td></tr>
              )}
            </tbody>
          </table>

          {historyNotifications.length > 0 && (
            <>
              <h4 className="detail-subtitle">历史代次通知记录</h4>
              <table className="data-table inner-table">
                <thead>
                  <tr><th>代次</th><th>渠道</th><th>状态</th><th>发送时间</th><th>取消时间</th></tr>
                </thead>
                <tbody>
                  {historyNotifications.map((item) => (
                    <tr key={item.id}>
                      <td>{item.执行代次 ?? '—'}</td>
                      <td>{item.渠道}</td>
                      <td><span className={`badge ${resultTone(item.状态)}`}>{item.状态}</span></td>
                      <td>{item.发送时间 ?? '—'}</td>
                      <td>{item.取消时间 ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </aside>
      )}
    </section>
  )
}
