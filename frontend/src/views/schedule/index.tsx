import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/schedule'
const columns = [
  "任务编号",
  "任务名称",
  "分析类型",
  "调度表达式",
  "通知渠道",
  "是否启用",
  "执行代次",
  "业务日期",
  "执行状态",
  "报告状态",
  "通知结果"
]
const actions = [
  "提交校验",
  "启用配置",
  "停用配置"
]
const statuses = [
  "草稿",
  "待校验",
  "已启用",
  "已停用"
]
const stats = [
  {
    "label": "定时任务总数",
    "value": 0
  },
  {
    "label": "待校验任务",
    "value": 0
  },
  {
    "label": "已启用任务",
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
  execution?: Execution | null
  executions?: Execution[]
}

function resultTone(result: string) {
  if (result === '已发送') return 'tone-ok'
  if (result === '部分失败' || result === '部分取消' || result === '待重试') return 'tone-warn'
  if (result === '发送失败' || result === '已取消') return 'tone-bad'
  return 'tone-muted'
}

export default function ScheduleView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [detail, setDetail] = useState<Row | null>(null)
  const filterFields = useMemo(() => [
    { label: '任务编号', query: 'keyword' },
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
        throw new Error('定时任务列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '定时分析任务列表读取失败')
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
        throw new Error('定时任务详情读取失败')
      }
      setDetail(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '定时任务详情读取失败')
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
        throw new Error(payload?.message || '定时分析任务动作未生效，请稍后重试')
      }
      setNotice(payload.message)
      await reload()
      if (detail?.id === row.id) {
        await openDetail(row)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '定时分析任务操作失败')
    }
  }

  return (
    <section className="page" data-module="schedule">
      <header className="page-head">
        <div>
          <h2>定时分析任务管理</h2>
          <p className="page-desc">
            维护调度配置与通知渠道。业务日期按执行开始时间归属，报告进入终态后才发送一次通知。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('定时任务登记入口尚未接入审批流')}>
            登记定时任务
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出定时分析任务清单
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
              placeholder={field.label === '状态' ? '如：已启用' : `按${field.label}检索`}
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
                if (column === '任务编号') {
                  return (
                    <td key={column}>
                      <button className="link" type="button" onClick={() => void openDetail(row)}>
                        {String(row[column] ?? '—')}
                      </button>
                    </td>
                  )
                }
                if (column === '通知结果') {
                  const result = String(row[column] ?? '—')
                  return <td key={column}><span className={`badge ${resultTone(result)}`}>{result}</span></td>
                }
                if (column === '执行代次') {
                  return <td key={column}>{row[column] === 0 ? '—' : String(row[column] ?? '—')}</td>
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
              <td className="empty-state" colSpan={columns.length + 1}>暂无定时分析任务数据，可先登记定时任务</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条定时分析任务记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      {detail && (
        <aside className="detail-panel">
          <header className="detail-head">
            <div>
              <h3>{String(detail["任务编号"])} · {String(detail["任务名称"])}</h3>
              <p className="page-desc">
                最后有效执行代次：{String(detail["执行代次"])}，业务日期：{String(detail["业务日期"])}
              </p>
            </div>
            <button className="btn ghost" type="button" onClick={() => setDetail(null)}>关闭</button>
          </header>

          {(detail.executions ?? (detail.execution ? [detail.execution] : [])).map((execution) => (
            <article key={execution.id} className="execution-card">
              <div className="execution-meta">
                <span>执行代次 <strong>{execution.执行代次}</strong></span>
                <span>业务日期 <strong>{execution.业务日期 || '—'}</strong></span>
                <span>开始 {execution.开始时间 || '—'}</span>
                <span>终态 {execution.终态时间 || '—'}</span>
                <span className={`badge ${resultTone(execution.通知结果)}`}>{execution.通知结果}</span>
              </div>
              <table className="data-table inner-table">
                <thead>
                  <tr><th>渠道</th><th>状态</th><th>尝试</th><th>发送时间</th><th>取消时间</th><th>说明</th><th>操作</th></tr>
                </thead>
                <tbody>
                  {execution.notifications.map((item) => (
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
                  {execution.notifications.length === 0 && (
                    <tr><td className="empty-state" colSpan={7}>该代次暂无通知记录</td></tr>
                  )}
                </tbody>
              </table>
            </article>
          ))}
          {!detail.execution && !(detail.executions ?? []).length && (
            <p className="empty-state">该任务还没有执行代次。</p>
          )}
        </aside>
      )}
    </section>
  )
}
