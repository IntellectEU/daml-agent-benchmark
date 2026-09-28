import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { Button, Card, Input, Modal, Popconfirm, Radio, Space, Switch, Table, Tooltip, Typography, message } from 'antd'
import type { ColumnType, ColumnsType } from 'antd/es/table'
import type { SortOrder, SorterResult } from 'antd/es/table/interface'

import { archiveAgentRuns, deleteAgentRuns, fetchAgentMatrix } from './api'
import { RunNameCell } from './components/RunNameCell'
import { formatDuration, formatMillionTokens, formatTestTally, formatTokenCount, formatUsd, readErrorDetail } from './format'
import { orderPinnedRuns, useRunPins } from './runPins'
import { RUN_COLUMN_META, STATUS_META, SYMBOL_MEANING, statusColumnLabel, statusEmoji } from './symbols'
import { TaskDetailModal } from './TaskDetailModal'
import { useClipboardFeedback } from './useClipboardFeedback'
import type {
  AgentMatrixResponse,
  AgentMatrixRunItem,
  AgentTaskCell,
  AgentTaskStatusCode,
} from './types'

const { Text } = Typography

const PIN_STORAGE_KEY = 'daml_agent_benchmark_pins'
const PACKAGE_TASKS_ONLY_KEY = 'daml_agent_benchmark_package_tasks_only'

// What the page shows until the first matrix response arrives.
const EMPTY_MATRIX: AgentMatrixResponse = {
  task_ids: [],
  task_header_colors: {},
  task_flags: {},
  package_task_ids: [],
  items: [],
}

// Column widths in pixels. The task columns share what is left of the container.
const RUN_NAME_WIDTH = 220
const PIN_WIDTH = 44
const CREATED_WIDTH = 96
const OUTCOME_WIDTH = 38
const TASK_TOTAL_WIDTH = 38
const TEST_SCRIPTS_WIDTH = 62
const DURATION_WIDTH = 54
const COST_WIDTH = 58
const TOKENS_WIDTH = 54
const ACTIONS_WIDTH = 84
const SELECTION_WIDTH = 46
const TASK_COL_MIN_WIDTH = 20
const TASK_COL_MAX_WIDTH = 32
// Every fixed column but the outcome columns, which come and go with the runs shown.
const FIXED_COLUMNS_BASE_WIDTH =
  PIN_WIDTH + RUN_NAME_WIDTH + CREATED_WIDTH + TASK_TOTAL_WIDTH + TEST_SCRIPTS_WIDTH + DURATION_WIDTH + COST_WIDTH +
  3 * TOKENS_WIDTH + ACTIONS_WIDTH + SELECTION_WIDTH

const PAGE_SIZE = 25
const PAGE_SIZE_OPTIONS = [10, 25, 50]

// The outcome columns in order: passed, then the failures, then what has not finished.
// Passed always shows; the others only when a run shown has a task with that outcome.
const OUTCOME_COLUMN_ORDER: AgentTaskStatusCode[] = [
  'success',
  'tests_failed',
  'build_failed',
  'parse_failed',
  'security_violation',
  'infra_failed',
  'usage_limit',
  'other_error',
  'running',
  'queued',
]

// A run that is still going has no wall time yet; the agents' time so far stands in.
function runDurationSeconds(row: AgentMatrixRunItem): number {
  return row.summary.run_wall_seconds ?? row.summary.agent_wall_seconds
}

function runUsdCost(row: AgentMatrixRunItem): number | null {
  return row.summary.total_usd_cost
}

// Incomplete usage means every total is what the run could account for, and no more.
function runCostIsLowerBound(row: AgentMatrixRunItem): boolean {
  return !row.summary.usage_complete
}

type TokenKind = 'fresh' | 'cached' | 'output'

function runTokenCount(row: AgentMatrixRunItem, kind: TokenKind): number {
  const usage = row.summary.usage
  if (kind === 'fresh') return Math.max(0, usage.input_tokens - usage.cached_input_tokens)
  if (kind === 'cached') return usage.cached_input_tokens
  return usage.output_tokens
}

// The run's cost per kind of token, summed over its tasks. Null when a task with a cost
// has no split, since the parts would then not add up to the total.
function runCostSplit(row: AgentMatrixRunItem): { fresh: number; cached: number; output: number } | null {
  const split = { fresh: 0, cached: 0, output: 0 }
  for (const cell of Object.values(row.task_statuses)) {
    if (cell.usd_cost === null) continue
    if (cell.input_cost === null || cell.cached_input_cost === null || cell.output_cost === null) return null
    split.fresh += cell.input_cost
    split.cached += cell.cached_input_cost
    split.output += cell.output_cost
  }
  return split
}

// Test scripts passed over all tasks. A run whose tasks report no scripts counts tasks instead.
function runTestScripts(row: AgentMatrixRunItem): { passed: number; total: number } {
  const cells = Object.values(row.task_statuses)
  const passed = cells.reduce((acc, cell) => acc + cell.tests_succeeded, 0)
  const total = cells.reduce((acc, cell) => acc + cell.tests_total, 0)
  if (total > 0) return { passed, total }
  return { passed: row.summary.tests_passed, total: row.summary.num_tasks }
}

function countFor(row: AgentMatrixRunItem, status: AgentTaskStatusCode): number {
  return row.status_counts[status] ?? 0
}

// The tasks evaluated: what the outcome columns add up to.
function runTaskTotal(row: AgentMatrixRunItem): number {
  return Object.values(row.status_counts).reduce((acc, count) => acc + (count ?? 0), 0)
}

function outcomeShare(row: AgentMatrixRunItem, status: AgentTaskStatusCode): number {
  const total = runTaskTotal(row)
  return total > 0 ? countFor(row, status) / total : 0
}

function testScriptShare(row: AgentMatrixRunItem): number {
  const { passed, total } = runTestScripts(row)
  return total > 0 ? passed / total : 0
}

type SizeKey = 'duration' | 'cost' | TokenKind

function runSize(row: AgentMatrixRunItem, key: SizeKey): number | null {
  if (key === 'duration') return runDurationSeconds(row)
  if (key === 'cost') return runUsdCost(row)
  return runTokenCount(row, key)
}

// The number a sortable column sorts by. Outcome and test columns sort by share.
function runSortValue(row: AgentMatrixRunItem, key: string): number {
  if (key.startsWith('status:')) return outcomeShare(row, key.slice('status:'.length) as AgentTaskStatusCode)
  if (key === 'task_total') return runTaskTotal(row)
  if (key === 'tests') return testScriptShare(row)
  return runSize(row, key as SizeKey) ?? -1
}

const SIZE_KEYS: SizeKey[] = ['duration', 'cost', 'fresh', 'cached', 'output']

function isRunSortKey(key: string): boolean {
  return (
    key === 'run_name' ||
    key === 'created_at' ||
    key === 'task_total' ||
    key === 'tests' ||
    key.startsWith('status:') ||
    (SIZE_KEYS as string[]).includes(key)
  )
}

type ShadeKind = 'good' | 'bad' | 'size'

// A tint whose strength follows t in [0, 1]. It is laid over the cell's own background,
// which a fixed column needs to stay opaque over the task columns scrolling beneath it.
function shadeStyle(kind: ShadeKind, t: number): CSSProperties {
  const percent = Math.round(6 + 52 * Math.max(0, Math.min(1, t)))
  const colour = `color-mix(in srgb, var(--run-shade-${kind}) ${percent}%, transparent)`
  return { backgroundImage: `linear-gradient(${colour}, ${colour})` }
}

// Where a value sits between the column's smallest and largest, from 0 to 1.
function rangePosition(value: number, range: { lo: number; hi: number }): number {
  return range.hi === range.lo ? 0.5 : (value - range.lo) / (range.hi - range.lo)
}

function ColumnHeader({ emoji, label }: { emoji: string; label: string }) {
  return (
    <Tooltip title={label} mouseEnterDelay={0} mouseLeaveDelay={0}>
      <span className="run-col-header" role="img" aria-label={label}>{emoji}</span>
    </Tooltip>
  )
}

// A count with its share of the run's tasks under it; a zero stays plain and muted.
function ShareCell({ count, total, unit }: { count: number; total: number; unit: string }) {
  if (count === 0) return <span className="run-col-muted">0</span>
  return (
    <Tooltip title={`${count} of ${total} ${unit}`} mouseEnterDelay={0.15} mouseLeaveDelay={0}>
      <div className="run-col-share">
        <span>{count}</span>
        <small>{total > 0 ? `${Math.round((100 * count) / total)}%` : '—'}</small>
      </div>
    </Tooltip>
  )
}

function formatCreatedAtDayFirst(createdAt: string): string {
  const date = new Date(createdAt)
  if (Number.isNaN(date.getTime())) return '—'
  const dd = String(date.getDate()).padStart(2, '0')
  const mm = String(date.getMonth() + 1).padStart(2, '0')
  const yyyy = date.getFullYear()
  const hh = String(date.getHours()).padStart(2, '0')
  const min = String(date.getMinutes()).padStart(2, '0')
  return `${dd}/${mm}/${yyyy} ${hh}:${min}`
}

function agentCreatedTimestampMs(run: AgentMatrixRunItem): number {
  return Date.parse(run.summary.created_at_utc)
}

function taskHeaderLabel(taskId: string): string {
  const parts = taskId.split('/')
  const file = parts[parts.length - 1] || taskId
  return file.replace(/\.daml$/i, '')
}

function repoHeaderStyle(taskId: string, taskHeaderColors: Record<string, string>) {
  const hex = taskHeaderColors[taskId]
  if (!hex) return {}
  return {
    backgroundColor: hex,
  }
}

function statusMeaning(cell: AgentTaskCell | undefined): string {
  if (!cell) return SYMBOL_MEANING['⚪']
  const label = STATUS_META[cell.status_code].label
  const status = cell.timed_out ? `${label}, timed out` : label
  const tally = formatTestTally(cell)
  return tally ? `${status} (${tally})` : status
}

function statusCellClass(cell: AgentTaskCell | undefined): string {
  if (!cell) return 'agent-matrix-cell-not-run'
  if (cell.status_code === 'queued') return 'agent-matrix-cell-not-run'
  if (cell.status_code === 'running') return 'agent-matrix-cell-running'
  if (cell.status_code === 'success') return 'agent-matrix-cell-success'
  if (cell.status_code === 'usage_limit') return 'agent-matrix-cell-usage-limit'
  return 'agent-matrix-cell-failed'
}


// The row as it would look with only the given tasks: cells dropped, and what can be
// recomputed from the remaining cells is. Duration and egress stay the whole run's.
// As on the server, only cells that have used tokens count. The cost is a lower bound
// when a task is still running or some tasks have no price, and unknown when none has one.
function restrictRow(row: AgentMatrixRunItem, keep: Set<string>): AgentMatrixRunItem {
  const task_statuses = Object.fromEntries(Object.entries(row.task_statuses).filter(([taskId]) => keep.has(taskId)))
  const cells = Object.values(task_statuses)
  const status_counts: AgentMatrixRunItem['status_counts'] = {}
  for (const cell of cells) status_counts[cell.status_code] = (status_counts[cell.status_code] ?? 0) + 1
  const counted = cells.filter((cell) => cell.input_tokens !== null)
  const sum = (pick: (cell: AgentTaskCell) => number | null) => counted.reduce((total, cell) => total + (pick(cell) ?? 0), 0)
  const priced = counted.filter((cell) => cell.usd_cost !== null)
  return {
    ...row,
    task_statuses,
    status_counts,
    summary: {
      ...row.summary,
      num_tasks: cells.length,
      tests_passed: status_counts.success ?? 0,
      total_usd_cost: priced.length > 0 ? sum((cell) => cell.usd_cost) : null,
      usage: {
        input_tokens: sum((cell) => cell.input_tokens),
        cached_input_tokens: sum((cell) => cell.cached_input_tokens),
        output_tokens: sum((cell) => cell.output_tokens),
      },
      usage_complete: priced.length === counted.length && !counted.some((cell) => cell.usd_cost_is_lower_bound),
    },
  }
}

type AgentViewMode = 'status' | 'cost'

export function AgentPage() {
  const tableContainerRef = useRef<HTMLDivElement | null>(null)
  const [matrix, setMatrix] = useState<AgentMatrixResponse>(EMPTY_MATRIX)
  const rows = matrix.items
  const allTaskIds = matrix.task_ids
  const packageTaskIds = matrix.package_task_ids
  const taskHeaderColors = matrix.task_header_colors
  // Flag -> severity, so a finding the page has not seen before still gets a colour.
  const taskFlags = matrix.task_flags
  const [packageTasksOnly, setPackageTasksOnly] = useState<boolean>(() => {
    try {
      return window.localStorage.getItem(PACKAGE_TASKS_ONLY_KEY) === '1'
    } catch {
      return false
    }
  })
  const packageTaskIdSet = useMemo(() => new Set(packageTaskIds), [packageTaskIds])
  // Runs over added tasklists show more than the package's tasks; only then is there a choice to offer.
  const hasTasksBeyondPackage = packageTaskIds.length > 0 && allTaskIds.some((taskId) => !packageTaskIdSet.has(taskId))
  const taskIds = useMemo(
    () => (packageTasksOnly && hasTasksBeyondPackage ? allTaskIds.filter((taskId) => packageTaskIdSet.has(taskId)) : allTaskIds),
    [allTaskIds, hasTasksBeyondPackage, packageTaskIdSet, packageTasksOnly]
  )
  useEffect(() => {
    try {
      window.localStorage.setItem(PACKAGE_TASKS_ONLY_KEY, packageTasksOnly ? '1' : '0')
    } catch {
      // Nothing to remember it in; the toggle just resets on the next load.
    }
  }, [packageTasksOnly])
  const [loading, setLoading] = useState(false)
  const [viewMode, setViewMode] = useState<AgentViewMode>('status')
  const [runSort, setRunSort] = useState<{ key: string; order: SortOrder }>({
    key: 'created_at',
    order: 'descend',
  })
  const [includeArchived, setIncludeArchived] = useState(false)
  const [selectedRunIds, setSelectedRunIds] = useState<string[]>([])
  const [archiveModalOpen, setArchiveModalOpen] = useState(false)
  const [archiveReason, setArchiveReason] = useState('')
  const [archiveSaving, setArchiveSaving] = useState(false)
  const [bulkDeletePending, setBulkDeletePending] = useState(false)
  const [deletingRunId, setDeletingRunId] = useState<string | null>(null)
  const [selectedTask, setSelectedTask] = useState<{ runId: string; taskId: string } | null>(null)
  const { copiedValue: copiedRunId, copyWithFeedback } = useClipboardFeedback()
  const [tableContainerWidth, setTableContainerWidth] = useState(0)
  const { pins, togglePin, isPinned } = useRunPins(PIN_STORAGE_KEY)

  const maxTaskCost = useMemo(() => {
    let max = 0
    for (const row of rows) {
      for (const cell of Object.values(row.task_statuses)) {
        if (cell.usd_cost && cell.usd_cost > max) {
          max = cell.usd_cost
        }
      }
    }
    return max || 1
  }, [rows])

  const getCostColor = useCallback((cost: number | null): string => {
    if (cost === null || cost === 0) return 'transparent'
    const ratio = Math.min(cost / maxTaskCost, 1)
    // One hue, pale gold for a cheap task and deep orange for the most expensive one.
    const h = 40 // constant hue (orange/gold)
    const s = 70 + 30 * ratio // saturation increases with cost
    const l = 95 - 40 * ratio // lightness decreases with cost
    return `hsl(${h}, ${s}%, ${l}%)`
  }, [maxTaskCost])

  const loadMatrix = useCallback(async (options?: { suppressError?: boolean }): Promise<boolean> => {
    setLoading(true)
    try {
      const res = await fetchAgentMatrix({ includeArchived })
      setMatrix(res)
      setSelectedRunIds((prev) => prev.filter((id) => res.items.some((run) => run.run_id === id)))
      return true
    } catch (err) {
      console.error(err)
      if (!options?.suppressError) {
        message.error(readErrorDetail(err, 'Failed to load agent matrix'))
      }
      return false
    } finally {
      setLoading(false)
    }
  }, [includeArchived])

  useEffect(() => {
    void loadMatrix()
  }, [loadMatrix])

  useEffect(() => {
    const element = tableContainerRef.current
    if (!element) return

    const updateWidth = () => {
      setTableContainerWidth(Math.floor(element.clientWidth))
    }

    updateWidth()
    const observer = new ResizeObserver(updateWidth)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  const rowsForDisplay = useMemo(() => {
    const restrict = packageTasksOnly && hasTasksBeyondPackage
    const out = restrict ? rows.map((row) => restrictRow(row, packageTaskIdSet)) : [...rows]
    if (runSort.order) {
      out.sort((a, b) => {
        let result: number
        if (runSort.key === 'run_name') {
          result = a.summary.run_name.localeCompare(b.summary.run_name)
        } else if (runSort.key === 'created_at') {
          const aTime = agentCreatedTimestampMs(a)
          const bTime = agentCreatedTimestampMs(b)
          result = aTime !== bTime ? aTime - bTime : a.run_id.localeCompare(b.run_id)
        } else {
          result = runSortValue(a, runSort.key) - runSortValue(b, runSort.key)
          if (result === 0) result = a.run_id.localeCompare(b.run_id)
        }
        return runSort.order === 'descend' ? -result : result
      })
    }
    return orderPinnedRuns(out, pins)
  }, [hasTasksBeyondPackage, packageTaskIdSet, packageTasksOnly, pins, rows, runSort])
  const selectedActiveRunIds = useMemo(
    () => selectedRunIds.filter((id) => rows.some((row) => row.run_id === id && !row.archived)),
    [rows, selectedRunIds]
  )

  // The outcome columns to show and each size column's range, over the runs shown.
  const runScales = useMemo(() => {
    const outcomes = OUTCOME_COLUMN_ORDER.filter(
      (status) => status === 'success' || rowsForDisplay.some((row) => countFor(row, status) > 0)
    )
    const ranges = {} as Record<SizeKey, { lo: number; hi: number }>
    for (const key of SIZE_KEYS) {
      const values = rowsForDisplay.map((row) => runSize(row, key)).filter((value): value is number => value !== null)
      ranges[key] = values.length > 0 ? { lo: Math.min(...values), hi: Math.max(...values) } : { lo: 0, hi: 0 }
    }
    return { outcomes, ranges }
  }, [rowsForDisplay])
  const fixedColumnsWidth = FIXED_COLUMNS_BASE_WIDTH + runScales.outcomes.length * OUTCOME_WIDTH

  const taskColumnWidth = useMemo(() => {
    if (taskIds.length === 0) return TASK_COL_MAX_WIDTH
    const available = tableContainerWidth > 0
      ? Math.max(tableContainerWidth - fixedColumnsWidth, TASK_COL_MIN_WIDTH * taskIds.length)
      : TASK_COL_MAX_WIDTH * taskIds.length
    const raw = Math.floor(available / taskIds.length)
    return Math.max(TASK_COL_MIN_WIDTH, Math.min(TASK_COL_MAX_WIDTH, raw))
  }, [fixedColumnsWidth, tableContainerWidth, taskIds.length])
  const tableScrollX = fixedColumnsWidth + taskIds.length * taskColumnWidth

  async function onDeleteSelected(): Promise<void> {
    if (selectedRunIds.length === 0) return
    const deleting = [...selectedRunIds]
    setBulkDeletePending(true)
    try {
      await deleteAgentRuns(deleting)
      setSelectedRunIds([])
      message.success('Deleted selected agent runs')
      await loadMatrix()
    } catch (err) {
      console.error(err)
      message.error(readErrorDetail(err, 'Failed to delete selected agent runs'))
    } finally {
      setBulkDeletePending(false)
    }
  }

  function onArchiveSelected(): void {
    if (selectedActiveRunIds.length === 0) return
    setArchiveModalOpen(true)
  }

  async function submitArchive(): Promise<void> {
    if (selectedActiveRunIds.length === 0) return
    setArchiveSaving(true)
    try {
      await archiveAgentRuns(selectedActiveRunIds, archiveReason)
      setSelectedRunIds([])
      setArchiveReason('')
      setArchiveModalOpen(false)
      message.success('Archived selected agent runs')
      await loadMatrix()
    } catch (err) {
      console.error(err)
      message.error(readErrorDetail(err, 'Failed to archive selected agent runs'))
    } finally {
      setArchiveSaving(false)
    }
  }

  const onDeleteRow = useCallback(async (runId: string): Promise<void> => {
    setDeletingRunId(runId)
    try {
      await deleteAgentRuns([runId])
      setSelectedRunIds((prev) => prev.filter((id) => id !== runId))
      message.success(`Deleted run ${runId}`)
      await loadMatrix()
    } catch (err) {
      console.error(err)
      message.error(readErrorDetail(err, `Failed to delete run ${runId}`))
    } finally {
      setDeletingRunId(null)
    }
  }, [loadMatrix])

  const onCopyRunId = useCallback(async (runId: string): Promise<void> => {
    const copied = await copyWithFeedback(runId)
    if (!copied) {
      message.error('Failed to copy Run ID')
    }
  }, [copyWithFeedback])

  const columns: ColumnsType<AgentMatrixRunItem> = useMemo(() => {
    // A narrow sortable column for one of the run's figures, headed by its symbol.
    const figureColumn = (
      key: string,
      width: number,
      header: { emoji: string; label: string },
      render: (row: AgentMatrixRunItem) => ReactNode,
      shade?: (row: AgentMatrixRunItem) => CSSProperties | undefined,
      extraClass = ''
    ): ColumnType<AgentMatrixRunItem> => ({
      title: <ColumnHeader emoji={header.emoji} label={header.label} />,
      key,
      width,
      fixed: 'left',
      align: 'center',
      className: `run-col-num ${extraClass}`.trim(),
      sorter: true,
      sortOrder: runSort.key === key ? runSort.order : null,
      onCell: (row) => ({ style: shade?.(row) }),
      render: (_, row) => render(row),
    })

    const sizeShade = (key: SizeKey) => (row: AgentMatrixRunItem) => {
      const value = runSize(row, key)
      return value === null ? undefined : shadeStyle('size', rangePosition(value, runScales.ranges[key]))
    }

    const tokenColumn = (kind: TokenKind) =>
      figureColumn(`${kind}`, TOKENS_WIDTH, RUN_COLUMN_META[kind], (row) => {
        const value = runTokenCount(row, kind)
        const isLowerBound = runCostIsLowerBound(row)
        return (
          <Tooltip title={`${formatTokenCount(value, isLowerBound)} tokens`} mouseEnterDelay={0.15} mouseLeaveDelay={0}>
            <span>{formatMillionTokens(value, isLowerBound)}</span>
          </Tooltip>
        )
      }, sizeShade(kind))

    const outcomeColumns = runScales.outcomes.map((status, idx) =>
      figureColumn(
        `status:${status}`,
        OUTCOME_WIDTH,
        { emoji: STATUS_META[status].emoji, label: statusColumnLabel(status) },
        (row) => <ShareCell count={countFor(row, status)} total={runTaskTotal(row)} unit="tasks" />,
        (row) => {
          if (countFor(row, status) === 0) return undefined
          const share = outcomeShare(row, status)
          // Failure shares rarely come near the whole run, so their scale tops out at 70%.
          return status === 'success' ? shadeStyle('good', share) : shadeStyle('bad', share / 0.7)
        },
        idx === 0 ? 'run-col-group-start' : ''
      )
    )

    // The "Tasks" label spans the outcome columns and Σ. It hangs above the first outcome
    // header, so every header stays in one row and keeps its vertical centring.
    const tasksGroupWidth = OUTCOME_WIDTH * outcomeColumns.length + TASK_TOTAL_WIDTH
    const [firstOutcome, ...otherOutcomes] = outcomeColumns
    const labelledFirstOutcome: ColumnType<AgentMatrixRunItem> = {
      ...firstOutcome,
      title: (
        <>
          <span className="run-col-group-label" style={{ width: tasksGroupWidth }}>Tasks</span>
          {firstOutcome.title as ReactNode}
        </>
      ),
      onHeaderCell: () => ({ className: 'run-col-group-label-host' }),
    }

    const figureColumns: ColumnsType<AgentMatrixRunItem> = [
      labelledFirstOutcome,
      ...otherOutcomes,
      figureColumn(
        'task_total',
        TASK_TOTAL_WIDTH,
        { emoji: 'Σ', label: 'Tasks in the run: the outcome columns add up to this' },
        (row) => runTaskTotal(row),
        undefined,
        'run-col-group-end'
      ),
      figureColumn('tests', TEST_SCRIPTS_WIDTH, RUN_COLUMN_META.tests, (row) => {
        const { passed, total } = runTestScripts(row)
        if (total === 0) return <span className="run-col-muted">—</span>
        return (
          <Tooltip title={`${passed} of ${total} test scripts`} mouseEnterDelay={0.15} mouseLeaveDelay={0}>
            <div className="run-col-share">
              <span>{passed}/{total}</span>
              <small>{Math.round((100 * passed) / total)}%</small>
            </div>
          </Tooltip>
        )
      }, (row) => (runTestScripts(row).total > 0 ? shadeStyle('good', testScriptShare(row)) : undefined)),
      figureColumn(
        'duration',
        DURATION_WIDTH,
        RUN_COLUMN_META.duration,
        (row) => formatDuration(runDurationSeconds(row)),
        sizeShade('duration')
      ),
      figureColumn('cost', COST_WIDTH, RUN_COLUMN_META.cost, (row) => {
        const cost = runUsdCost(row)
        const isLowerBound = runCostIsLowerBound(row)
        if (cost === null) return <span className="run-col-muted">—</span>
        const split = runCostSplit(row)
        const tooltipTitle = (
          <div className="run-cost-split">
            <strong>Uncached:</strong>
            <span>{formatTokenCount(runTokenCount(row, 'fresh'), isLowerBound)}</span>
            <span>{split ? formatUsd(split.fresh, isLowerBound) : '—'}</span>
            <strong>Cached:</strong>
            <span>{formatTokenCount(runTokenCount(row, 'cached'), isLowerBound)}</span>
            <span>{split ? formatUsd(split.cached, isLowerBound) : '—'}</span>
            <strong>Output:</strong>
            <span>{formatTokenCount(runTokenCount(row, 'output'), isLowerBound)}</span>
            <span>{split ? formatUsd(split.output, isLowerBound) : '—'}</span>
          </div>
        )
        return (
          <Tooltip title={tooltipTitle} mouseEnterDelay={0} mouseLeaveDelay={0.1}>
            <span>{formatUsd(cost, isLowerBound)}</span>
          </Tooltip>
        )
      }, sizeShade('cost')),
      tokenColumn('fresh'),
      tokenColumn('cached'),
      tokenColumn('output'),
    ]

    const base: ColumnsType<AgentMatrixRunItem> = [
      {
        title: 'Pin',
        key: 'pin',
        width: PIN_WIDTH,
        fixed: 'left',
        render: (_, row) => {
          const pinTitle = isPinned(row.run_id) ? 'Unpin this run' : 'Pin this run to the top'
          return (
            <Tooltip title={pinTitle} mouseEnterDelay={0.15} mouseLeaveDelay={0}>
              <Button size="small" aria-label={pinTitle} onClick={() => togglePin(row.run_id)}>
                {isPinned(row.run_id) ? '★' : '☆'}
              </Button>
            </Tooltip>
          )
        },
      },
      {
        title: 'Run Name',
        key: 'run_name',
        width: RUN_NAME_WIDTH,
        fixed: 'left',
        sorter: true,
        sortOrder: runSort.key === 'run_name' ? runSort.order : null,
        render: (_, row) => {
          return (
            <RunNameCell
              runId={row.run_id}
              runName={row.summary.run_name}
              copiedRunId={copiedRunId}
              onCopyRunId={onCopyRunId}
              tags={[
                ...(row.archived
                  ? [{ key: 'archived', label: 'Archived', color: 'gold' }]
                  : []),
                ...(row.summary.status === 'in_progress'
                  ? [{ key: 'in_progress', label: 'In Progress', color: 'processing' }]
                  : []),
              ]}
            />
          )
        },
      },
      {
        title: 'Created',
        key: 'created_at',
        width: CREATED_WIDTH,
        fixed: 'left',
        sorter: true,
        sortOrder: runSort.key === 'created_at' ? runSort.order : null,
        render: (_, row) => formatCreatedAtDayFirst(row.summary.created_at_utc),
      },
      ...figureColumns,
      {
        title: 'Actions',
        key: 'actions',
        width: ACTIONS_WIDTH,
        fixed: 'left',
        render: (_, row) => (
          <Space direction="vertical" size={2}>
            {!row.archived && (
              <Button
                size="small"
                className="agent-row-action"
                onClick={() => {
                  // The archive dialog works on the selection, so this row becomes it.
                  setSelectedRunIds([row.run_id])
                  setArchiveModalOpen(true)
                }}
              >
                Archive
              </Button>
            )}
            <Popconfirm
              title={`Delete ${row.run_id}?`}
              description="This removes the run from active and archived agent logs."
              okText="Delete"
              okButtonProps={{ danger: true }}
              onConfirm={() => void onDeleteRow(row.run_id)}
            >
              <Button size="small" danger className="agent-row-action" loading={deletingRunId === row.run_id}>
                Delete
              </Button>
            </Popconfirm>
          </Space>
        ),
      },
    ]

    const taskColumns: ColumnsType<AgentMatrixRunItem> = taskIds.map((taskId, idx) => {
      const isFirstTask = idx === 0
      const isLastTask = idx === taskIds.length - 1
      const headerClass = [
        'agent-matrix-status-header',
        isFirstTask ? 'agent-matrix-status-header-start' : '',
        isLastTask ? 'agent-matrix-status-header-end' : '',
      ]
        .filter(Boolean)
        .join(' ')

      return {
        title: (
          <Tooltip
            title={taskId}
            mouseEnterDelay={0}
            mouseLeaveDelay={0.4}
            overlayClassName="agent-matrix-header-tooltip"
          >
            <div className="agent-matrix-header">
              {taskHeaderLabel(taskId)}
            </div>
          </Tooltip>
        ),
        key: `task:${taskId}`,
        width: taskColumnWidth,
        align: 'center',
        className: 'agent-matrix-status-cell',
        onHeaderCell: () => ({ className: headerClass, style: repoHeaderStyle(taskId, taskHeaderColors) }),
        onCell: (row) => {
          // A run that never had this task has no cell for it.
          const cell: AgentTaskCell | undefined = row.task_statuses[taskId]
          const style: CSSProperties = {}
          if (viewMode === 'cost' && cell && cell.usd_cost) {
            style.backgroundColor = getCostColor(cell.usd_cost)
          }
          return {
            className: `agent-matrix-status-cell ${statusCellClass(cell)}`,
            style,
          }
        },
        render: (_, row) => {
          const cell: AgentTaskCell | undefined = row.task_statuses[taskId]
          const statusTitle = `${statusMeaning(cell)} · click to open the task`

          let content: ReactNode
          if (viewMode === 'cost') {
            const usdCost = cell === undefined ? null : cell.usd_cost
            const costIsLowerBound = cell !== undefined && cell.usd_cost_is_lower_bound
            const displayCost = usdCost !== null ? (
              <div className="agent-matrix-cost">
                <div className="agent-matrix-cost-symbol">$</div>
                <div>{`${costIsLowerBound ? '>' : ''}${usdCost.toFixed(2)}`}</div>
              </div>
            ) : (
              <div className="agent-matrix-cost">—</div>
            )

            // The token counts come as a set: either the attempt reported usage or it did not.
            const nonCachedTokens = cell !== undefined && cell.input_tokens !== null && cell.cached_input_tokens !== null
              ? Math.max(0, cell.input_tokens - cell.cached_input_tokens)
              : null
            const tooltipTitle = cell !== undefined && usdCost !== null ? (
              <div style={{ fontSize: '12px' }}>
                <div style={{ marginBottom: 4 }}>{statusTitle}</div>
                <div style={{ display: 'grid', gridTemplateColumns: 'auto 1fr auto', gap: '4px 12px' }}>
                  <strong>Uncached:</strong> <span style={{ textAlign: 'right' }}>{formatTokenCount(nonCachedTokens)}</span> <span style={{ textAlign: 'right' }}>{formatUsd(cell.input_cost, costIsLowerBound)}</span>
                  <strong>Cached:</strong> <span style={{ textAlign: 'right' }}>{formatTokenCount(cell.cached_input_tokens)}</span> <span style={{ textAlign: 'right' }}>{formatUsd(cell.cached_input_cost, costIsLowerBound)}</span>
                  <strong>Output:</strong> <span style={{ textAlign: 'right' }}>{formatTokenCount(cell.output_tokens)}</span> <span style={{ textAlign: 'right' }}>{formatUsd(cell.output_cost, costIsLowerBound)}</span>
                </div>
              </div>
            ) : 'No cost data'

            content = (
              <Tooltip title={tooltipTitle} mouseEnterDelay={0} mouseLeaveDelay={0.1}>
                {displayCost}
              </Tooltip>
            )
          } else {
            const emoji = statusEmoji(cell)
            content = (
              <Tooltip title={statusTitle} mouseEnterDelay={0.15} mouseLeaveDelay={0}>
                <span className="agent-matrix-emoji">{emoji}</span>
              </Tooltip>
            )
          }

          return (
            <button
              type="button"
              className="agent-matrix-cell-btn"
              aria-label={`${statusTitle} ${taskId}`}
              onClick={() => setSelectedTask({ runId: row.run_id, taskId })}
            >
              {content}
            </button>
          )
        },
      }
    })

    return [...base, ...taskColumns]
  }, [
    copiedRunId,
    deletingRunId,
    getCostColor,
    isPinned,
    onCopyRunId,
    onDeleteRow,
    runScales,
    runSort,
    taskColumnWidth,
    taskHeaderColors,
    taskIds,
    togglePin,
    viewMode,
  ])

  function onRunTableChange(
    _pagination: unknown,
    _filters: unknown,
    sorter: SorterResult<AgentMatrixRunItem> | SorterResult<AgentMatrixRunItem>[]
  ): void {
    const activeSorter = Array.isArray(sorter) ? sorter[0] : sorter
    const columnKey = activeSorter?.columnKey === undefined ? '' : String(activeSorter.columnKey)
    if (!isRunSortKey(columnKey)) return
    setRunSort({ key: columnKey, order: activeSorter.order ?? null })
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      <Card>
        <Space wrap size={12}>
          <Button onClick={() => void loadMatrix()} loading={loading}>
            Refresh
          </Button>
          <span className="label-wrap">
            Include archived <Switch checked={includeArchived} onChange={setIncludeArchived} />
          </span>
          {hasTasksBeyondPackage && (
            <Tooltip
              title="Only the tasks that ship with the package: what a user of the public benchmark sees. Each row's counts, cost and tokens are recomputed over those tasks; duration stays the whole run's."
              mouseEnterDelay={0.15}
              mouseLeaveDelay={0}
            >
              <span className="label-wrap">
                Package tasks only <Switch checked={packageTasksOnly} onChange={setPackageTasksOnly} />
              </span>
            </Tooltip>
          )}
          <span className="label-wrap">
            View:
            <Radio.Group
              value={viewMode}
              onChange={(e) => setViewMode(e.target.value)}
              size="small"
              optionType="button"
              buttonStyle="solid"
              style={{ marginLeft: 8 }}
              options={[
                { value: 'status', label: 'Status' },
                { value: 'cost', label: 'Cost' },
              ]}
            />
          </span>
          <Button disabled={selectedActiveRunIds.length === 0} onClick={onArchiveSelected}>            Archive ({selectedActiveRunIds.length})
          </Button>
          <Popconfirm
            title={`Delete ${selectedRunIds.length} run(s)?`}
            description="This removes selected runs from active and archived agent logs."
            okText="Delete"
            okButtonProps={{ danger: true, loading: bulkDeletePending }}
            onConfirm={() => void onDeleteSelected()}
            disabled={selectedRunIds.length === 0}
          >
            <Button danger disabled={selectedRunIds.length === 0} loading={bulkDeletePending}>
              Delete ({selectedRunIds.length})
            </Button>
          </Popconfirm>
          <Text type="secondary">
            Matrix size: {rowsForDisplay.length} {rowsForDisplay.length === 1 ? 'run' : 'runs'} × {taskIds.length}{' '}
            {taskIds.length === 1 ? 'task' : 'tasks'}
          </Text>
        </Space>
      </Card>

      <Card title="Runs by task">
        <div ref={tableContainerRef}>
          <Table<AgentMatrixRunItem>
            rowKey="run_id"
            dataSource={rowsForDisplay}
            columns={columns}
            loading={loading}
            tableLayout="fixed"
            rowSelection={{
              columnWidth: SELECTION_WIDTH,
              selectedRowKeys: selectedRunIds,
              onChange: (keys) => setSelectedRunIds(keys.map(String)),
            }}
            pagination={{ pageSize: PAGE_SIZE, showSizeChanger: true, pageSizeOptions: PAGE_SIZE_OPTIONS }}
            onChange={onRunTableChange}
            scroll={{ x: tableScrollX }}
            size="small"
          />
        </div>
      </Card>

      <Modal
        title={`Archive ${selectedActiveRunIds.length} run(s)`}
        open={archiveModalOpen}
        onCancel={() => setArchiveModalOpen(false)}
        onOk={() => void submitArchive()}
        okButtonProps={{ loading: archiveSaving }}
        okText="Archive"
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Text type="secondary">Optional archive reason</Text>
          <Input.TextArea
            rows={4}
            value={archiveReason}
            onChange={(e) => setArchiveReason(e.target.value)}
            placeholder="Reason"
          />
        </Space>
      </Modal>

      {selectedTask && (
        <TaskDetailModal
          runId={selectedTask.runId}
          taskId={selectedTask.taskId}
          taskFlags={taskFlags}
          onClose={() => setSelectedTask(null)}
        />
      )}
    </Space>
  )
}
