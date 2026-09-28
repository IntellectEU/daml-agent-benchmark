// The dialog that opens from a matrix cell. It shows how the task was graded, the Codex
// event timeline with its token-usage chart, the implementation files before and after,
// the terminal log and the raw JSON. It loads the task itself and polls while the task is running.
// Mount it while a task is selected and unmount it to close it. Filters, sort order and
// expanded sections are state of this component, so closing discards them.

import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { Button, Modal, Select, Space, Tag, Tooltip, Typography, message } from 'antd'

import { fetchAgentTaskDetail } from './api'
import { copyTextToClipboard } from './clipboard'
import { CodeBlock } from './components/CodeBlock'
import { Sym } from './components/Sym'
import { formatDuration, formatSmallUsd, formatTestTally, formatTokenCount, readErrorDetail } from './format'
import { STATUS_META } from './symbols'
import { useClipboardFeedback } from './useClipboardFeedback'
import type {
  AgentRequestCost,
  AgentCostCategory,
  AgentEventCosts,
  AgentItemCost,
  AgentSeverity,
  AgentStdoutEvent,
  AgentTaskDetailResponse,
} from './types'

const { Text } = Typography

// How often the detail of a task that is still running is fetched again.
const LIVE_POLL_INTERVAL_MS = 1500

const FLAG_COLORS: Record<AgentSeverity, string> = {
  security: 'red',
  infra: 'orange',
  warning: 'gold',
}

function parseInlineMarkdown(text: string): ReactNode[] {
  const out: ReactNode[] = []
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)/g
  let lastIdx = 0
  let keyIdx = 0

  for (const match of text.matchAll(pattern)) {
    const token = match[0]
    const start = match.index ?? 0
    if (start > lastIdx) {
      out.push(<span key={`txt-${keyIdx++}`}>{text.slice(lastIdx, start)}</span>)
    }
    if (token.startsWith('`') && token.endsWith('`')) {
      out.push(
        <code key={`code-${keyIdx++}`} className="agent-md-inline-code">
          {token.slice(1, -1)}
        </code>
      )
    } else if (token.startsWith('**') && token.endsWith('**')) {
      out.push(<strong key={`b-${keyIdx++}`}>{token.slice(2, -2)}</strong>)
    } else if (token.startsWith('*') && token.endsWith('*')) {
      out.push(<em key={`i-${keyIdx++}`}>{token.slice(1, -1)}</em>)
    } else {
      out.push(<span key={`tok-${keyIdx++}`}>{token}</span>)
    }
    lastIdx = start + token.length
  }

  if (lastIdx < text.length) {
    out.push(<span key={`tail-${keyIdx++}`}>{text.slice(lastIdx)}</span>)
  }
  return out
}

function renderMarkdownBlock(text: string): ReactNode {
  const lines = text.split('\n')
  const blocks: ReactNode[] = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]
    const trimmed = line.trim()

    if (!trimmed) {
      i += 1
      continue
    }

    if (trimmed.startsWith('```')) {
      const language = trimmed.slice(3).trim() || 'text'
      const codeLines: string[] = []
      i += 1
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        codeLines.push(lines[i])
        i += 1
      }
      if (i < lines.length) i += 1
      blocks.push(
        <CodeBlock
          key={`code-block-${blocks.length}`}
          language={language}
          text={codeLines.join('\n')}
          maxHeight="260px"
          margin="8px 0"
        />
      )
      continue
    }

    if (trimmed.startsWith('- ')) {
      const items: string[] = []
      while (i < lines.length) {
        const bullet = lines[i].trim()
        if (!bullet.startsWith('- ')) break
        items.push(bullet.slice(2))
        i += 1
      }
      blocks.push(
        <ul key={`list-${blocks.length}`} className="agent-md-list">
          {items.map((item, idx) => (
            <li key={`li-${idx}`}>{parseInlineMarkdown(item)}</li>
          ))}
        </ul>
      )
      continue
    }

    const paragraphLines: string[] = []
    while (i < lines.length) {
      const current = lines[i].trim()
      if (!current || current.startsWith('- ') || current.startsWith('```')) break
      paragraphLines.push(current)
      i += 1
    }
    blocks.push(
      <p key={`p-${blocks.length}`} className="agent-md-paragraph">
        {parseInlineMarkdown(paragraphLines.join(' '))}
      </p>
    )
  }

  return <div className="agent-md-block">{blocks}</div>
}

type ParsedCodexStdoutLine = {
  lineNo: number
  raw: string
  event: Record<string, unknown> | null
  rawEvent: Record<string, unknown> | null
  capturedAtUtc: string | null
  eventDurationSeconds: number | null
  itemDurationSeconds: number | null
  totalDurationSeconds: number | null
  fileChangeDiffs: Array<{
    path: string
    kind: string
    beforeMissing: boolean
    afterMissing: boolean
    diffUnified: string | null
  }>
}

type CodexTimelineEntry = {
  line: ParsedCodexStdoutLine
  pairedStartLineNo: number | null
}

// The events between two consecutive requests, with the tokens the second one used.
type TokenUsageEventWindow = {
  key: string
  startLineExclusive: number | null
  endLineInclusive: number
  eventInputTokens: number
  eventOutputTokens: number
  cacheMissTokens: number
  hasDamlTestCommand: boolean
}

type EventSortKey = 'line' | 'duration' | 'cost'

// A row of the timeline: an event, or a cache miss shown just before the request it belongs to.
type TimelineRow =
  | { kind: 'event'; entry: CodexTimelineEntry; cost: AgentItemCost | null; request: AgentRequestCost | null }
  | { kind: 'miss'; request: AgentRequestCost }

const COST_CATEGORY_META: Record<AgentCostCategory, { label: string; color: string }> = {
  user_message: { label: 'user_message', color: '#2563eb' },
  reasoning: { label: 'reasoning', color: '#7c3aed' },
  agent_message: { label: 'agent_message', color: '#16a34a' },
  file_change: { label: 'file_change', color: '#0d9488' },
  command_execution: { label: 'command_execution', color: '#ea580c' },
  cache_miss: { label: 'cache_miss', color: '#dc2626' },
  unattributed: { label: 'unattributed', color: '#94a3b8' },
}

function formatShare(part: number, total: number): string {
  if (total <= 0 || part <= 0) return '0%'
  const share = (100 * part) / total
  return share < 1 ? '<1%' : `${Math.round(share)}%`
}

const THREAD_TOKEN_USAGE_EVENT_TYPES = new Set([
  'thread.token_usage.updated',
])

function isThreadTokenUsageEventType(eventType: string): boolean {
  return THREAD_TOKEN_USAGE_EVENT_TYPES.has(eventType)
}

function codexFilterableKindForEvent(event: Record<string, unknown> | null): string | null {
  if (!event) return 'non-json'
  const item = event.item && typeof event.item === 'object'
    ? event.item as Record<string, unknown>
    : null
  const itemType = item && typeof item.type === 'string' ? item.type : null
  if (itemType) return itemType
  const eventType = typeof event.type === 'string' ? event.type : ''
  if (isThreadTokenUsageEventType(eventType)) {
    return 'thread.token_usage.updated'
  }
  return null
}

// A parsed line while the timeline is being built: the timestamp in milliseconds
// serves the duration arithmetic and is dropped from the result.
type ParsingCodexStdoutLine = ParsedCodexStdoutLine & { capturedAtMs: number | null }

// A line with nothing known about it beyond its number and text.
function emptyParsedLine(lineNo: number, raw: string): ParsingCodexStdoutLine {
  return {
    lineNo,
    raw,
    event: null,
    rawEvent: null,
    capturedAtUtc: null,
    capturedAtMs: null,
    eventDurationSeconds: null,
    itemDurationSeconds: null,
    totalDurationSeconds: null,
    fileChangeDiffs: [],
  }
}

// The event records are the timeline.
// The records of the last attempt. A retried attempt starts its line numbers again, so a
// stream whose numbers restart holds several attempts. Only the last one counts, as in the pricing.
function lastAttemptRecords(records: AgentStdoutEvent[]): AgentStdoutEvent[] {
  let start = 0
  for (let i = 1; i < records.length; i += 1) {
    if (records[i].line_no <= records[i - 1].line_no) start = i
  }
  return records.slice(start)
}

function parseCodexStdoutLines(eventRecords: AgentStdoutEvent[]): ParsedCodexStdoutLine[] {
  const entries: ParsingCodexStdoutLine[] = []

  for (const rec of lastAttemptRecords(eventRecords)) {
    const capturedAtMs = Date.parse(rec.captured_at_utc)
    entries.push({
      ...emptyParsedLine(rec.line_no, JSON.stringify(rec.event)),
      event: rec.event,
      rawEvent: rec.raw_event,
      capturedAtUtc: rec.captured_at_utc,
      capturedAtMs: Number.isNaN(capturedAtMs) ? null : capturedAtMs,
      fileChangeDiffs: (rec.file_change_diffs ?? []).map((item) => ({
        path: item.path,
        kind: item.kind,
        beforeMissing: item.before_missing,
        afterMissing: item.after_missing,
        diffUnified: item.diff_unified,
      })),
    })
  }

  entries.sort((a, b) => a.lineNo - b.lineNo)

  const runStartMs = entries.find((entry) => entry.capturedAtMs !== null)?.capturedAtMs ?? null
  const itemStartedAtMs = new Map<string, number>()
  let prevEventMs: number | null = null

  for (const entry of entries) {
    const eventMs = entry.capturedAtMs
    if (eventMs !== null) {
      if (runStartMs !== null) {
        entry.totalDurationSeconds = Math.max(0, (eventMs - runStartMs) / 1000)
      }
      if (prevEventMs !== null) {
        entry.eventDurationSeconds = Math.max(0, (eventMs - prevEventMs) / 1000)
      }
      prevEventMs = eventMs
    }

    if (!entry.event) continue
    const eventType = typeof entry.event.type === 'string' ? entry.event.type : ''
    const item = entry.event.item && typeof entry.event.item === 'object'
      ? entry.event.item as Record<string, unknown>
      : null
    const itemId = item && typeof item.id === 'string' ? item.id : null

    if (eventType === 'item.started' && itemId && eventMs !== null) {
      itemStartedAtMs.set(itemId, eventMs)
      continue
    }

    if (eventType === 'item.completed' && eventMs !== null) {
      if (itemId && itemStartedAtMs.has(itemId)) {
        const startedAtMs = itemStartedAtMs.get(itemId) as number
        entry.itemDurationSeconds = Math.max(0, (eventMs - startedAtMs) / 1000)
        itemStartedAtMs.delete(itemId)
      }
    }
  }

  return entries.map(({ capturedAtMs: _capturedAtMs, ...entry }) => entry)
}

/**
 * Build timeline rows for display from parsed Codex stdout lines.
 *
 * We collapse noisy start/end pairs by:
 * - tracking `item.started` + `item.completed` for item rows by `(item.type, item.id)`
 * - hiding the paired `item.started` row
 * - keeping the `item.completed` row and attaching `pairedStartLineNo`
 *
 * The UI then shows labels like `#<start> -> #<completed>` so lifecycle context is preserved
 * without duplicating rows.
 */
function buildCodexTimelineEntries(lines: ParsedCodexStdoutLine[]): CodexTimelineEntry[] {
  const startedLineByItemKey = new Map<string, number>()
  const pairedStartedLineNos = new Set<number>()
  const pairedStartByCompletedLineNo = new Map<number, number>()
  const isDeltaEventType = (eventType: string): boolean => eventType.toLowerCase().includes('delta')

  for (const line of lines) {
    const event = line.event
    if (!event) continue
    const eventType = typeof event.type === 'string' ? event.type : ''
    if (isDeltaEventType(eventType)) continue
    const item = event.item && typeof event.item === 'object'
      ? event.item as Record<string, unknown>
      : null
    const itemType = item && typeof item.type === 'string' ? item.type : ''
    const itemId = item && typeof item.id === 'string' ? item.id : ''
    if (!itemId) continue
    const itemKey = `${itemType}:${itemId}`

    if (eventType === 'item.started') {
      startedLineByItemKey.set(itemKey, line.lineNo)
      continue
    }

    if (eventType === 'item.completed') {
      const startedLineNo = startedLineByItemKey.get(itemKey)
      if (typeof startedLineNo === 'number') {
        pairedStartedLineNos.add(startedLineNo)
        pairedStartByCompletedLineNo.set(line.lineNo, startedLineNo)
        startedLineByItemKey.delete(itemKey)
      }
    }
  }

  const out: CodexTimelineEntry[] = []
  for (const line of lines) {
    const eventType = line.event && typeof line.event.type === 'string' ? line.event.type : ''
    if (isDeltaEventType(eventType)) continue
    if (pairedStartedLineNos.has(line.lineNo)) continue
    out.push({
      line,
      pairedStartLineNo: pairedStartByCompletedLineNo.get(line.lineNo) ?? null,
    })
  }
  return out
}

function codexEventTagColor(eventType: string): string {
  switch (eventType) {
    case 'thread.started':
    case 'turn.started':
      return 'blue'
    case 'turn.completed':
      return 'green'
    case 'turn.failed':
    case 'error':
      return 'red'
    case 'item.started':
      return 'processing'
    case 'item.completed':
      return 'cyan'
    case 'item.updated':
      return 'purple'
    default:
      return 'default'
  }
}

function codexStatusTagColor(status: string): string {
  switch (status) {
    case 'completed':
      return 'green'
    case 'failed':
      return 'red'
    case 'in_progress':
      return 'processing'
    default:
      return 'default'
  }
}

function codexItemTypeTagColor(itemType: string): string {
  switch (itemType) {
    case 'command_execution':
      return 'volcano'
    case 'webSearch':
    case 'web_search':
      return 'magenta'
    case 'local_shell_call':
      return 'orange'
    case 'reasoning':
      return 'purple'
    case 'agent_message':
    case 'message':
    case 'assistant_message':
      return 'cyan'
    case 'user_message':
      return 'blue'
    case 'function_call':
    case 'tool_call':
      return 'gold'
    case 'non-json':
      return 'default'
    default:
      return 'geekblue'
  }
}

function basenameFromPath(path: string): string {
  const trimmed = path.trim()
  if (!trimmed) return ''
  const normalized = trimmed.replace(/\\/g, '/')
  const parts = normalized.split('/').filter((part) => part.length > 0)
  if (parts.length === 0) return normalized
  return parts[parts.length - 1]
}

type SideBySideDiffRow = {
  leftLine: string
  rightLine: string
  leftKind: 'same' | 'delete' | 'modify' | 'empty'
  rightKind: 'same' | 'insert' | 'modify' | 'empty'
}

function buildSideBySideDiffRows(originalText: string, generatedText: string): SideBySideDiffRow[] {
  const left = originalText.split('\n')
  const right = generatedText.split('\n')
  const n = left.length
  const m = right.length
  const lcs: number[][] = Array.from({ length: n + 1 }, () => Array<number>(m + 1).fill(0))

  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      if (left[i] === right[j]) {
        lcs[i][j] = lcs[i + 1][j + 1] + 1
      } else {
        lcs[i][j] = Math.max(lcs[i + 1][j], lcs[i][j + 1])
      }
    }
  }

  const ops: Array<{ type: 'equal' | 'delete' | 'insert'; line: string }> = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (left[i] === right[j]) {
      ops.push({ type: 'equal', line: left[i] })
      i += 1
      j += 1
      continue
    }
    if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      ops.push({ type: 'delete', line: left[i] })
      i += 1
    } else {
      ops.push({ type: 'insert', line: right[j] })
      j += 1
    }
  }
  while (i < n) {
    ops.push({ type: 'delete', line: left[i] })
    i += 1
  }
  while (j < m) {
    ops.push({ type: 'insert', line: right[j] })
    j += 1
  }

  const rows: SideBySideDiffRow[] = []
  let cursor = 0
  while (cursor < ops.length) {
    const op = ops[cursor]
    if (op.type === 'equal') {
      rows.push({
        leftLine: op.line,
        rightLine: op.line,
        leftKind: 'same',
        rightKind: 'same',
      })
      cursor += 1
      continue
    }

    const deletes: string[] = []
    const inserts: string[] = []
    while (cursor < ops.length && ops[cursor].type !== 'equal') {
      if (ops[cursor].type === 'delete') {
        deletes.push(ops[cursor].line)
      } else {
        inserts.push(ops[cursor].line)
      }
      cursor += 1
    }

    const pairs = Math.min(deletes.length, inserts.length)
    for (let idx = 0; idx < pairs; idx += 1) {
      rows.push({
        leftLine: deletes[idx],
        rightLine: inserts[idx],
        leftKind: 'modify',
        rightKind: 'modify',
      })
    }
    for (let idx = pairs; idx < deletes.length; idx += 1) {
      rows.push({
        leftLine: deletes[idx],
        rightLine: '',
        leftKind: 'delete',
        rightKind: 'empty',
      })
    }
    for (let idx = pairs; idx < inserts.length; idx += 1) {
      rows.push({
        leftLine: '',
        rightLine: inserts[idx],
        leftKind: 'empty',
        rightKind: 'insert',
      })
    }
  }

  return rows
}

function diffRowsToAlignedText(rows: SideBySideDiffRow[]): {
  leftText: string
  rightText: string
  leftKinds: Array<SideBySideDiffRow['leftKind']>
  rightKinds: Array<SideBySideDiffRow['rightKind']>
} {
  const leftLines: string[] = []
  const rightLines: string[] = []
  const leftKinds: Array<SideBySideDiffRow['leftKind']> = []
  const rightKinds: Array<SideBySideDiffRow['rightKind']> = []
  for (const row of rows) {
    leftLines.push(row.leftLine)
    rightLines.push(row.rightLine)
    leftKinds.push(row.leftKind)
    rightKinds.push(row.rightKind)
  }
  return {
    leftText: leftLines.join('\n'),
    rightText: rightLines.join('\n'),
    leftKinds,
    rightKinds,
  }
}

function syntaxLanguageForImpl(path: string): string {
  return /\.daml$/i.test(path) ? 'haskell' : 'text'
}

// A cost as the timeline shows it: dollars when the model has a price, else tokens.
// An estimated figure is marked with a leading ~.
function formatCostValue(value: number | null, priced: boolean, approximate = false): string {
  const text = priced ? formatSmallUsd(value) : `${formatTokenCount(value)} tokens`
  return approximate && value !== null ? `~${text}` : text
}

// Why a per-event figure is marked ~, shown on hover.
const APPROXIMATE_NOTE =
  'Estimated. The task total and each request\'s cost are exact: they come from the token counts the API reports. ' +
  'How a request\'s tokens divide among the events around it is estimated. Events between the same two requests ' +
  'share their tokens by text length. After the agent compacts the conversation, events it summarised away keep a ' +
  'share of later resends from cache.'

// Categories that are not sums of per-event estimates.
const EXACT_CATEGORIES: ReadonlySet<AgentCostCategory> = new Set(['cache_miss', 'unattributed'])

// What an item cost, in words: how many tokens it added and what each part of its cost was.
function costSplitText(cost: AgentItemCost, priced: boolean): string {
  // The first user_message also carries Codex's own instructions, which the timeline does not show.
  const label = cost.category === 'user_message' ? 'Task prompt and agent instructions' : COST_CATEGORY_META[cost.category].label
  const parts = [label, `~${formatTokenCount(cost.tokens)} tokens into the conversation`]
  const resends = `${cost.resends} resend${cost.resends === 1 ? '' : 's'}`
  if (priced) {
    if (cost.written_tokens > 0) parts.push(`written ${formatCostValue(cost.written_usd, true, true)}`)
    parts.push(`first send ${formatCostValue(cost.first_send_usd, true, true)}`)
    if (cost.resends > 0) parts.push(`${resends} ${formatCostValue(cost.resend_usd, true, true)}`)
  } else {
    if (cost.written_tokens > 0) parts.push(`${formatTokenCount(cost.written_tokens)} written`)
    if (cost.resends > 0) parts.push(resends)
  }
  return parts.join(' · ')
}

type CostChipProps = {
  value: number | null
  priced: boolean
  // The largest cost in the timeline, which a full bar stands for.
  maxCost: number
  // The task's total, which the share is of.
  total: number
  miss?: boolean
}

// A cache miss is exact; an event's cost is an estimate and is marked as one.
function CostChip({ value, priced, maxCost, total, miss = false }: CostChipProps) {
  const amount = value ?? 0
  const width = maxCost > 0 ? Math.max(0, Math.min(100, (100 * amount) / maxCost)) : 0
  return (
    <span className={`agent-cost-chip${miss ? ' agent-cost-chip-miss' : ''}`} title={miss ? undefined : APPROXIMATE_NOTE}>
      <span className="agent-cost-bar"><i style={{ width: `${width}%` }} /></span>
      <span className="agent-cost-amount">{formatCostValue(value, priced, !miss)}</span>
      <span className="agent-cost-share">{formatShare(amount, total)}</span>
    </span>
  )
}

// The task's cost by kind of event: a stacked bar and a legend with amounts and shares.
function CostSummaryStrip({ eventCosts, total }: { eventCosts: AgentEventCosts; total: number }) {
  const value = (category: AgentEventCosts['categories'][number]) => (eventCosts.priced ? category.usd ?? 0 : category.tokens)
  const shown = eventCosts.categories.filter((category) => value(category) > 0).sort((a, b) => value(b) - value(a))
  return (
    <div className="agent-cost-summary">
      <div className="agent-cost-summary-head">
        <strong>Task cost {eventCosts.priced ? formatSmallUsd(eventCosts.total_usd) : '—'}</strong>
        <Text type="secondary">
          {eventCosts.priced
            ? `${eventCosts.requests.length} requests at ${eventCosts.model} prices. Each event carries what it cost to write, to send once fresh and to resend from cache.`
            : eventCosts.model === null
              ? 'The run was billed to a subscription, so no cost is shown and the timeline shows tokens only.'
              : `No price is known for ${eventCosts.model}, so the timeline shows tokens only.`}
        </Text>
      </div>
      <div className="agent-cost-stack" role="img" aria-label="Task cost by kind of event">
        {shown.map((category) => (
          <i
            key={category.key}
            style={{ width: `${total > 0 ? (100 * value(category)) / total : 0}%`, background: COST_CATEGORY_META[category.key].color }}
          />
        ))}
      </div>
      <div className="agent-cost-legend">
        {shown.map((category) => (
          <span key={category.key} title={EXACT_CATEGORIES.has(category.key) ? undefined : APPROXIMATE_NOTE}>
            <i style={{ background: COST_CATEGORY_META[category.key].color }} />
            {COST_CATEGORY_META[category.key].label}{' '}
            <b>{formatCostValue(value(category), eventCosts.priced, !EXACT_CATEGORIES.has(category.key))}</b> {formatShare(value(category), total)}
          </span>
        ))}
      </div>
    </div>
  )
}

// A request as a thin divider under the items it produced.
function RequestDivider({ request, priced }: { request: AgentRequestCost; priced: boolean }) {
  return (
    <div className="agent-request">
      <span className="agent-request-label">{request.sub_agent ? 'Sub-agent request' : 'Request'} {request.index}</span>
      <span className="agent-request-nums">
        {formatTokenCount(request.fresh)} fresh · {formatTokenCount(request.cached)} cached · {formatTokenCount(request.out)} out
        {request.unattributed_tokens > 0 && (
          <> · unattributed {priced ? formatSmallUsd(request.unattributed_usd) : `${formatTokenCount(request.unattributed_tokens)} tokens`}</>
        )}
      </span>
      {priced && <span className="agent-request-amount">{formatSmallUsd(request.usd)}</span>}
    </div>
  )
}

// The extra a request paid for tokens that should have come from cache, just before that request.
function CacheMissRow({ request, priced, maxCost, total }: { request: AgentRequestCost; priced: boolean; maxCost: number; total: number }) {
  return (
    <div className="agent-codex-event agent-cache-miss">
      <div className="agent-codex-event-summary">
        <Tag color="red">cache miss</Tag>
        <span>Cache miss before request {request.index}</span>
        <CostChip value={priced ? request.miss_usd : request.miss_tokens} priced={priced} maxCost={maxCost} total={total} miss />
        <span className="agent-cost-split">
          {formatTokenCount(request.miss_tokens)} tokens that should have come from cache were billed fresh
        </span>
      </div>
    </div>
  )
}

type TaskDetailModalProps = {
  runId: string
  taskId: string
  // Flag -> severity, for colouring the audit findings.
  taskFlags: Record<string, AgentSeverity>
  onClose: () => void
}

export function TaskDetailModal({ runId, taskId, taskFlags, onClose }: TaskDetailModalProps) {
  const [detailLoading, setDetailLoading] = useState(true)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [detailData, setDetailData] = useState<AgentTaskDetailResponse | null>(null)
  const [selectedImplPath, setSelectedImplPath] = useState<string | null>(null)
  const { copiedValue: copiedTaskJsonPath, copyWithFeedback: copyTaskJsonPathWithFeedback } = useClipboardFeedback()
  const [eventSortKey, setEventSortKey] = useState<EventSortKey>('line')
  const [eventSortDirection, setEventSortDirection] = useState<'asc' | 'desc'>('asc')
  const [eventItemTypeFilter, setEventItemTypeFilter] = useState<string | null>(null)
  const [selectedTokenUsageWindowKey, setSelectedTokenUsageWindowKey] = useState<string | null>(null)
  const [rawEventModal, setRawEventModal] = useState<{ lineNo: number; eventType: string; text: string } | null>(null)
  const [showTerminalLogText, setShowTerminalLogText] = useState(false)
  const [showRawTaskJson, setShowRawTaskJson] = useState(false)

  function defaultEventSortDirection(key: EventSortKey): 'asc' | 'desc' {
    return key === 'line' ? 'asc' : 'desc'
  }

  function onEventSortKeyClick(nextKey: EventSortKey): void {
    if (nextKey === eventSortKey) {
      setEventSortDirection((prev) => (prev === 'asc' ? 'desc' : 'asc'))
      return
    }
    setEventSortKey(nextKey)
    setEventSortDirection(defaultEventSortDirection(nextKey))
  }

  const attempt = detailData?.task.attempts ?? null
  const grade = detailData?.task.grade ?? null
  const codexStderrSource = attempt?.stderr || ''
  // A task still running has no attempt yet; its events come from the file the driver streams into.
  const codexEvents = useMemo(
    () => attempt?.stdout_events ?? detailData?.live_events ?? [],
    [attempt, detailData]
  )
  const parsedCodexStdoutLines = useMemo(
    () => parseCodexStdoutLines(codexEvents),
    [codexEvents]
  )
  const parsedCodexStdoutCount = useMemo(
    () => parsedCodexStdoutLines.filter((line) => !!line.event).length,
    [parsedCodexStdoutLines]
  )
  const eventCosts: AgentEventCosts | null = detailData?.event_costs ?? null
  // One bar per request, as the pricing counts them: repeated reports dropped, cache misses per thread.
  const tokenUsageEventWindows = useMemo(() => {
    const out: TokenUsageEventWindow[] = []
    let previousLineNo: number | null = null
    for (const request of eventCosts?.requests ?? []) {
      const start = previousLineNo
      const hasDamlTestCommand = parsedCodexStdoutLines.some((line) => {
        if (line.lineNo > request.line_no || (start !== null && line.lineNo <= start)) return false
        const event = line.event
        if (!event || event.type !== 'item.completed') return false
        const item = event.item && typeof event.item === 'object'
          ? event.item as Record<string, unknown>
          : null
        if (!item || String(item.type || '') !== 'command_execution') return false
        const command = typeof item.command === 'string' ? item.command : ''
        return /\bdaml\s+test\b/i.test(command)
      })
      out.push({
        key: `token-usage-window-${request.line_no}`,
        startLineExclusive: start,
        endLineInclusive: request.line_no,
        eventInputTokens: request.fresh,
        eventOutputTokens: request.out,
        cacheMissTokens: request.miss_tokens,
        hasDamlTestCommand,
      })
      previousLineNo = request.line_no
    }
    return out
  }, [eventCosts, parsedCodexStdoutLines])
  const selectedTokenUsageWindow = useMemo(
    () => tokenUsageEventWindows.find((usageWindow) => usageWindow.key === selectedTokenUsageWindowKey) ?? null,
    [selectedTokenUsageWindowKey, tokenUsageEventWindows]
  )
  const tokenUsageMaxStackedDelta = useMemo(() => {
    let max = 0
    for (const usageWindow of tokenUsageEventWindows) {
      max = Math.max(max, usageWindow.eventInputTokens + usageWindow.eventOutputTokens)
    }
    return max > 0 ? max : 1
  }, [tokenUsageEventWindows])
  const requestCostsByLine = useMemo(
    () => new Map((eventCosts?.requests ?? []).map((request) => [request.line_no, request])),
    [eventCosts]
  )
  // What sorting by cost ranks rows by: the cost when the model has a price, else the tokens.
  const rowCostMetric = useMemo(() => {
    const priced = eventCosts?.priced ?? false
    return (row: TimelineRow): number => {
      if (row.kind === 'miss') return (priced ? row.request.miss_usd : row.request.miss_tokens) ?? 0
      if (row.cost === null) return 0
      return (priced ? row.cost.total_usd : row.cost.tokens) ?? 0
    }
  }, [eventCosts])
  const codexTimelineRows = useMemo(
    () => {
      const rows: TimelineRow[] = []
      for (const entry of buildCodexTimelineEntries(parsedCodexStdoutLines)) {
        const request = requestCostsByLine.get(entry.line.lineNo) ?? null
        if (request !== null && request.miss_tokens > 0) rows.push({ kind: 'miss', request })
        rows.push({ kind: 'event', entry, cost: eventCosts?.items[String(entry.line.lineNo)] ?? null, request })
      }
      if (eventSortKey === 'line') {
        return eventSortDirection === 'asc' ? rows : [...rows].reverse()
      }
      // Sorting by cost lists what cost something: items and cache misses, not the requests.
      const out = eventSortKey === 'cost'
        ? rows.filter((row) => row.kind === 'miss' || (row.request === null && rowCostMetric(row) > 0))
        : rows.filter((row): row is Extract<TimelineRow, { kind: 'event' }> => row.kind === 'event')
      const metricValue = (row: TimelineRow): number => {
        if (eventSortKey === 'cost') return rowCostMetric(row)
        if (row.kind === 'miss') return -1
        return row.entry.line.itemDurationSeconds ?? row.entry.line.eventDurationSeconds ?? -1
      }
      const rowLineNo = (row: TimelineRow): number => (row.kind === 'miss' ? row.request.line_no : row.entry.line.lineNo)
      out.sort((a, b) => {
        const aMetric = metricValue(a)
        const bMetric = metricValue(b)
        if (aMetric !== bMetric) {
          return eventSortDirection === 'asc' ? aMetric - bMetric : bMetric - aMetric
        }
        return rowLineNo(a) - rowLineNo(b)
      })
      return out
    },
    [requestCostsByLine, eventCosts, eventSortDirection, eventSortKey, parsedCodexStdoutLines, rowCostMetric]
  )
  const availableItemTypes = useMemo(() => {
    const types = new Set<string>()
    for (const row of codexTimelineRows) {
      const kind = row.kind === 'event' ? codexFilterableKindForEvent(row.entry.line.event) : null
      if (kind) types.add(kind)
    }
    return Array.from(types).sort()
  }, [codexTimelineRows])
  const filteredCodexTimelineRows = useMemo(() => {
    return codexTimelineRows.filter((row) => {
      if (row.kind === 'miss' && eventItemTypeFilter) return false
      if (row.kind === 'event' && eventItemTypeFilter && codexFilterableKindForEvent(row.entry.line.event) !== eventItemTypeFilter) {
        return false
      }
      if (!selectedTokenUsageWindow) return true
      const lineNo = row.kind === 'miss' ? row.request.line_no : row.entry.line.lineNo
      if (lineNo > selectedTokenUsageWindow.endLineInclusive) return false
      if (selectedTokenUsageWindow.startLineExclusive !== null && lineNo <= selectedTokenUsageWindow.startLineExclusive) {
        return false
      }
      return true
    })
  }, [codexTimelineRows, eventItemTypeFilter, selectedTokenUsageWindow])
  // The largest item or miss, which a full cost bar stands for.
  const maxRowCost = useMemo(
    () => Math.max(0, ...codexTimelineRows.filter((row) => row.kind === 'miss' || row.request === null).map(rowCostMetric)),
    [codexTimelineRows, rowCostMetric]
  )
  const taskCostTotal = eventCosts === null
    ? 0
    : eventCosts.priced
      ? eventCosts.total_usd ?? 0
      : eventCosts.categories.reduce((sum, category) => sum + category.tokens, 0)
  const implFileViews = useMemo(() => detailData?.impl_file_views || [], [detailData])
  const selectedImplView = useMemo(() => {
    if (implFileViews.length === 0) return null
    const selected = selectedImplPath
      ? implFileViews.find((item) => item.impl_file === selectedImplPath) || null
      : null
    return selected || implFileViews[0]
  }, [implFileViews, selectedImplPath])
  const implDiffRows = useMemo(
    () => buildSideBySideDiffRows(selectedImplView?.original_text || '', selectedImplView?.generated_text || ''),
    [selectedImplView]
  )
  const alignedImplDiff = useMemo(() => diffRowsToAlignedText(implDiffRows), [implDiffRows])

  async function onCopyTaskJsonPath(taskJsonPath: string): Promise<void> {
    const copied = await copyTaskJsonPathWithFeedback(taskJsonPath)
    if (!copied) {
      message.error('Failed to copy Task JSON path')
    }
  }

  // Load the task once, then poll only while the response says it is still running.
  // A finished task never changes, so its detail is fetched once.
  useEffect(() => {
    let active = true
    let timer: number | undefined
    let lastDetail: AgentTaskDetailResponse | null = null

    setDetailLoading(true)
    setDetailError(null)
    setDetailData(null)

    const load = async () => {
      try {
        const detail = await fetchAgentTaskDetail(runId, taskId)
        if (!active) return
        lastDetail = detail
        setDetailData(detail)
        setDetailError(null)
      } catch (err) {
        if (!active) return
        console.error(err)
        setDetailError(readErrorDetail(
          err,
          lastDetail ? 'Failed to refresh task details' : 'Failed to load task details'
        ))
      } finally {
        if (active) setDetailLoading(false)
      }
      // A failed poll keeps polling; the last response still said the task is live.
      if (lastDetail?.is_live) {
        timer = window.setTimeout(() => void load(), LIVE_POLL_INTERVAL_MS)
      }
    }

    void load()

    return () => {
      active = false
      window.clearTimeout(timer)
    }
  }, [runId, taskId])

  return (
    <>
      <Modal
        title={detailData ? `Task Details: ${detailData.task_id}` : 'Task Details'}
        open
        onCancel={onClose}
        footer={(
          <Button onClick={onClose}>
            Close
          </Button>
        )}
        width="92vw"
        style={{ top: 20 }}
      >
        {detailLoading && <Text>Loading task details...</Text>}
        {!detailLoading && detailError && (
          <div className="agent-task-detail-error">{detailError}</div>
        )}
        {!detailLoading && detailData && (
          <Space className="agent-task-detail-layout" direction="vertical" size={12} style={{ width: '100%' }}>
            <div className="agent-task-detail-top-row">
              <div className="agent-task-detail-block">
                <div className="agent-task-detail-title">Overview</div>
                <div className="agent-task-detail-grid">
                  <div><strong>Status</strong></div>
                  <div>
                    {STATUS_META[detailData.cell.status_code].emoji}
                    {detailData.cell.timed_out && <Sym s="⌛" />}
                    {' '}
                    {STATUS_META[detailData.cell.status_code].label}
                    {formatTestTally(detailData.cell) && ` (${formatTestTally(detailData.cell)})`}
                  </div>
                  <div><strong>Live</strong></div>
                  <div>{detailData.is_live ? `Yes (${detailData.task.live_state})` : 'No'}</div>
                  <div><strong>Run ID</strong></div>
                  <div><Text code>{detailData.run_id}</Text></div>
                  <div><strong>Task ID</strong></div>
                  <div><Text code>{detailData.task_id}</Text></div>
                  <div><strong>Task JSON</strong></div>
                  <div>
                    <Space size={6} align="center">
                      <Text code>{detailData.task_file_path}</Text>
                      <Button
                        size="small"
                        aria-label={copiedTaskJsonPath === detailData.task_file_path ? 'Task JSON path copied' : 'Copy task JSON path'}
                        onClick={() => void onCopyTaskJsonPath(detailData.task_file_path)}
                      >
                        {copiedTaskJsonPath === detailData.task_file_path ? '✅' : '📋'}
                      </Button>
                    </Space>
                  </div>
                </div>
              </div>

              <div className="agent-task-detail-block">
                <div className="agent-task-detail-title">Result</div>
                <div className="agent-task-detail-grid">
                  <div><strong>Checks</strong></div>
                  <div className="agent-task-result-badges">
                    {grade ? (
                      <>
                        <Tag color={grade.syntax_passed ? 'green' : 'red'}>
                          Syntax: {grade.syntax_passed ? 'Pass' : 'Fail'}
                        </Tag>
                        <Tag color={grade.compile_passed ? 'green' : 'red'}>
                          Compile: {grade.compile_passed ? 'Pass' : 'Fail'}
                        </Tag>
                        <Tag color={grade.tests_passed ? 'green' : 'red'}>
                          Tests: {grade.tests_passed ? 'Pass' : 'Fail'}
                        </Tag>
                      </>
                    ) : (
                      <Tag>Not graded</Tag>
                    )}
                  </div>
                  <div><strong>Started (UTC)</strong></div>
                  <div>{detailData.task.started_at_utc || '—'}</div>
                  <div><strong>Model</strong></div>
                  <div>{attempt?.model || '—'}</div>
                  <div><strong>Return Code</strong></div>
                  <div>{attempt?.returncode ?? '—'}</div>
                  <div><strong>Agent Wall</strong></div>
                  <div>{formatDuration(attempt?.wall_seconds ?? null)}</div>
                  <div><strong>Attempts</strong></div>
                  <div>{attempt ? attempt.quota_retry_count + 1 : '—'}</div>
                </div>
              </div>
            </div>

            <div className="agent-task-detail-block agent-task-detail-block-final-message">
              <div className="agent-task-detail-title">Final Message from Agent</div>
              {renderMarkdownBlock(attempt?.final_message || '—')}
            </div>

            <div className="agent-task-detail-block agent-task-detail-block-per-test">
              <div className="agent-task-detail-title">Per-Test Outcomes</div>
              <div className="agent-task-detail-grid">
                <div><strong>Entries</strong></div>
                <div>
                  {Object.keys(grade?.test_results || {}).length}
                </div>
                <div><strong>Values</strong></div>
                <div>
                  {Object.entries(grade?.test_results || {}).length === 0 && '—'}
                  {Object.entries(grade?.test_results || {}).map(([name, ok]) => (
                    <div key={name}>
                      <Text code>{name}</Text> <Sym s={ok ? '✅' : '❌'} meaning={ok ? 'Script passed' : 'Script failed'} />
                    </div>
                  ))}
                </div>
              </div>
            </div>

            <div className="agent-task-detail-block agent-task-detail-block-paths">
              <div className="agent-task-detail-title">Paths</div>
              <div className="agent-task-detail-grid">
                <div><strong>Test File</strong></div>
                <div><Text code>{detailData.task.test_file}</Text></div>
                <div><strong>Impl Files</strong></div>
                <div>
                  {detailData.task.impl_files.length === 0 && '—'}
                  {detailData.task.impl_files.map((path) => (
                    <div key={path}><Text code>{path}</Text></div>
                  ))}
                </div>
              </div>
            </div>

            <div className="agent-task-detail-block agent-task-detail-block-impl-viewer">
              <div className="agent-task-detail-title">Model Response by Impl File</div>
              {implFileViews.length === 0 && (
                <Text type="secondary">
                  No per-file generated snapshots are stored for this task.
                </Text>
              )}
              {implFileViews.length > 0 && selectedImplView && (
                <>
                  <div className="agent-impl-viewer-toolbar">
                    <div className="agent-impl-viewer-select">
                      <strong>Impl file</strong>
                      <Select
                        value={selectedImplView.impl_file}
                        style={{ minWidth: 460, maxWidth: '100%' }}
                        options={implFileViews.map((item) => ({
                          label: item.path_in_copy,
                          value: item.impl_file,
                        }))}
                        onChange={(value: string) => setSelectedImplPath(value)}
                        showSearch
                        optionFilterProp="label"
                      />
                    </div>
                    <div className="agent-impl-viewer-tags">
                      <Tag>Original: {selectedImplView.original_source}</Tag>
                      <Tag>Generated: {selectedImplView.generated_source}</Tag>
                    </div>
                  </div>
                  <div className="agent-impl-viewer-path">
                    <Text code>{selectedImplView.impl_file}</Text>
                  </div>
                  {selectedImplView.generated_text === null && (
                    <div className="agent-task-detail-error">
                      Model-generated content is missing for this file in stored metadata.
                    </div>
                  )}
                  <div className="agent-impl-diff-grid">
                    <div className="agent-impl-diff-col">
                      <div className="agent-impl-diff-header">Original</div>
                      <CodeBlock
                        language={syntaxLanguageForImpl(selectedImplView.impl_file)}
                        text={alignedImplDiff.leftText}
                        theme="light"
                        maxHeight="520px"
                        lineClassName={(lineNumber) => `agent-impl-diff-line-${alignedImplDiff.leftKinds[lineNumber - 1] || 'same'}`}
                      />
                    </div>
                    <div className="agent-impl-diff-col">
                      <div className="agent-impl-diff-header">Model Generated</div>
                      <CodeBlock
                        language={syntaxLanguageForImpl(selectedImplView.impl_file)}
                        text={alignedImplDiff.rightText}
                        theme="light"
                        maxHeight="520px"
                        lineClassName={(lineNumber) => `agent-impl-diff-line-${alignedImplDiff.rightKinds[lineNumber - 1] || 'same'}`}
                      />
                    </div>
                  </div>
                </>
              )}
            </div>

            <div className="agent-task-detail-block agent-task-detail-block-errors">
              <div className="agent-task-detail-title">Findings</div>
              <div className="agent-task-detail-grid">
                <div><strong>Audit</strong></div>
                <div>
                  {detailData.task.findings.length === 0 && 'None'}
                  {detailData.task.findings.map((finding) => (
                    <div key={`${finding.flag}:${finding.detail}`}>
                      <Tag color={FLAG_COLORS[taskFlags[finding.flag]]}>{finding.flag}</Tag>
                      <span className="agent-task-detail-pre">{finding.detail}</span>
                    </div>
                  ))}
                </div>
                <div><strong>Syntax Error</strong></div>
                <div className="agent-task-detail-pre">{grade?.syntax_error || '—'}</div>
                <div><strong>Compile Error</strong></div>
                <div className="agent-task-detail-pre">{grade?.compile_error || '—'}</div>
                <div><strong>Tests Error</strong></div>
                <div className="agent-task-detail-pre">{grade?.tests_error || '—'}</div>
              </div>
            </div>

            <div className="agent-task-detail-block agent-task-detail-block-terminal">
              <div className="agent-task-detail-title">Terminal Overview</div>
              <div className="agent-task-detail-grid">
                <div><strong>Log Path</strong></div>
                <div><Text code>{detailData.terminal_log.path || 'No split task log found'}</Text></div>
                <div><strong>Total Chars</strong></div>
                <div>{detailData.terminal_log.total_chars}</div>
                <div><strong>Truncated</strong></div>
                <div>{detailData.terminal_log.truncated ? 'Yes' : 'No'}</div>
                <div><strong>Log Text</strong></div>
                <div>
                  <details
                    onToggle={(evt) => {
                      const target = evt.currentTarget as HTMLDetailsElement
                      setShowTerminalLogText(target.open)
                    }}
                  >
                    <summary>
                      {showTerminalLogText ? 'Hide log text' : 'Show log text'}
                    </summary>
                    {showTerminalLogText && (
                      <pre className="agent-task-detail-pre">
                        {detailData.terminal_log.content || '—'}
                      </pre>
                    )}
                  </details>
                </div>
              </div>
            </div>

            <div className="agent-task-detail-block agent-task-detail-block-token-usage">
              <div className="agent-task-detail-title">Thread Token Usage Deltas</div>
              {tokenUsageEventWindows.length === 0 && (
                <Text type="secondary">No requests reported.</Text>
              )}
              {tokenUsageEventWindows.length > 0 && (
                <div>
                  <Space wrap size={8} style={{ marginBottom: 8 }}>
                    <Tag>requests: {tokenUsageEventWindows.length}</Tag>
                    {selectedTokenUsageWindow && (
                      <Tag color="blue">
                        window lines #{selectedTokenUsageWindow.startLineExclusive === null ? 1 : selectedTokenUsageWindow.startLineExclusive + 1}{' -> '}#{selectedTokenUsageWindow.endLineInclusive}
                      </Tag>
                    )}
                    {selectedTokenUsageWindow && (
                      <Button size="small" onClick={() => setSelectedTokenUsageWindowKey(null)}>
                        clear window filter
                      </Button>
                    )}
                  </Space>
                  <div className="agent-token-usage-legend">
                    <span className="agent-token-usage-legend-item">
                      <span className="agent-token-usage-legend-swatch agent-token-usage-legend-swatch-input" />
                      input delta
                    </span>
                    <span className="agent-token-usage-legend-item">
                      <span className="agent-token-usage-legend-swatch agent-token-usage-legend-swatch-output" />
                      output delta
                    </span>
                    <span className="agent-token-usage-legend-item">
                      <span className="agent-token-usage-legend-swatch agent-token-usage-legend-swatch-cache-drop" />
                      cache miss
                    </span>
                    <span className="agent-token-usage-legend-item">
                      <span className="agent-token-usage-legend-text-marker">T</span>
                      daml test completed
                    </span>
                  </div>
                  <div className="agent-token-usage-chart-scroll">
                    <div className="agent-token-usage-plot">
                      <div className="agent-token-usage-guide-line agent-token-usage-guide-line-top">
                        <span className="agent-token-usage-guide-label">
                          {formatTokenCount(tokenUsageMaxStackedDelta)}
                        </span>
                      </div>
                      <div className="agent-token-usage-chart">
                        {tokenUsageEventWindows.map((usageWindow) => {
                          const selected = selectedTokenUsageWindow?.key === usageWindow.key
                          const inputHeightPct = Math.round((usageWindow.eventInputTokens / tokenUsageMaxStackedDelta) * 100)
                          const outputHeightPct = Math.round((usageWindow.eventOutputTokens / tokenUsageMaxStackedDelta) * 100)
                          const labelStart = usageWindow.startLineExclusive === null ? 1 : usageWindow.startLineExclusive + 1
                          const label = `lines ${labelStart}-${usageWindow.endLineInclusive}`
                          const hoverTitle = `event #${usageWindow.endLineInclusive} | ${label} | in=${usageWindow.eventInputTokens} out=${usageWindow.eventOutputTokens}`
                          return (
                            <Tooltip key={usageWindow.key} title={hoverTitle} mouseEnterDelay={0} mouseLeaveDelay={0}>
                              <button
                                type="button"
                                aria-label={hoverTitle}
                                aria-pressed={selected}
                                className={`agent-token-usage-bucket${selected ? ' agent-token-usage-bucket-selected' : ''}`}
                                onClick={() => setSelectedTokenUsageWindowKey((prev) => prev === usageWindow.key ? null : usageWindow.key)}
                              >
                                <div className="agent-token-usage-bars">
                                  <div
                                    className="agent-token-usage-bar agent-token-usage-bar-input"
                                    style={{ height: `${inputHeightPct}%` }}
                                  />
                                  <div
                                    className="agent-token-usage-bar agent-token-usage-bar-output"
                                    style={{ height: `${outputHeightPct}%` }}
                                  />
                                  {usageWindow.cacheMissTokens > 0 ? (
                                    <Tooltip
                                      title={`cache miss: ${formatTokenCount(usageWindow.cacheMissTokens)}`}
                                      mouseEnterDelay={0}
                                      mouseLeaveDelay={0}
                                    >
                                      <span
                                        className="agent-token-usage-cache-drop-bar"
                                        style={{
                                          height: `${Math.min(100, Math.round((usageWindow.cacheMissTokens / tokenUsageMaxStackedDelta) * 100))}%`,
                                        }}
                                      />
                                    </Tooltip>
                                  ) : (
                                    ''
                                  )}
                                </div>
                                <div className="agent-token-usage-test-marker">
                                  {usageWindow.hasDamlTestCommand ? 'T' : ''}
                                </div>
                              </button>
                            </Tooltip>
                          )
                        })}
                      </div>
                    </div>
                  </div>
                </div>
              )}
            </div>

            <div className="agent-task-detail-block agent-task-detail-block-timeline">
              <div className="agent-task-detail-title">Event Timeline</div>
              {eventCosts !== null && eventCosts.requests.length > 0 && (
                <CostSummaryStrip eventCosts={eventCosts} total={taskCostTotal} />
              )}
              <div className="agent-task-detail-grid">
                <div><strong>Timestamped Events</strong></div>
                <div>{codexEvents.length}</div>
                <div><strong>Stdout Lines</strong></div>
                <div>
                  {parsedCodexStdoutLines.length} (JSON parsed: {parsedCodexStdoutCount})
                </div>
                <div><strong>Events</strong></div>
                <div>
                  <Space wrap size={8} style={{ marginBottom: 8 }}>
                    <Button size="small" type={eventSortKey === 'line' ? 'primary' : 'default'} onClick={() => onEventSortKeyClick('line')}>
                      order
                    </Button>
                    <Button size="small" type={eventSortKey === 'duration' ? 'primary' : 'default'} onClick={() => onEventSortKeyClick('duration')}>
                      duration
                    </Button>
                    <Button size="small" type={eventSortKey === 'cost' ? 'primary' : 'default'} onClick={() => onEventSortKeyClick('cost')}>
                      cost
                    </Button>
                    <span style={{ borderLeft: '1px solid #d9d9d9', height: 16, display: 'inline-block', margin: '0 4px', verticalAlign: 'middle' }} />
                    {availableItemTypes.map((t) => {
                      const active = eventItemTypeFilter === t
                      const toggle = () => setEventItemTypeFilter(active ? null : t)
                      return (
                        <Tag
                          key={t}
                          role="button"
                          tabIndex={0}
                          aria-pressed={active}
                          color={active ? codexItemTypeTagColor(t) : undefined}
                          style={{ cursor: 'pointer', userSelect: 'none', opacity: eventItemTypeFilter && !active ? 0.4 : 1 }}
                          onClick={toggle}
                          onKeyDown={(evt) => {
                            if (evt.key === 'Enter' || evt.key === ' ') {
                              evt.preventDefault()
                              toggle()
                            }
                          }}
                        >
                          {t}
                        </Tag>
                      )
                    })}
                    {eventItemTypeFilter && (
                      <Button size="small" onClick={() => setEventItemTypeFilter(null)}>
                        clear filter
                      </Button>
                    )}
                    {selectedTokenUsageWindow && (
                      <Tag color="blue">
                        lines {(selectedTokenUsageWindow.startLineExclusive === null ? 1 : selectedTokenUsageWindow.startLineExclusive + 1)}-{selectedTokenUsageWindow.endLineInclusive}
                      </Tag>
                    )}
                  </Space>
                  {parsedCodexStdoutLines.length === 0 && '—'}
                  {parsedCodexStdoutLines.length > 0 && filteredCodexTimelineRows.length === 0 && (
                    <Text type="secondary">No timeline events match the active filters.</Text>
                  )}
                  {parsedCodexStdoutLines.length > 0 && (
                    <div className="agent-codex-events">
                      {filteredCodexTimelineRows.map((row) => {
                        if (row.kind === 'miss') {
                          return (
                            <CacheMissRow
                              key={`cache-miss-${row.request.line_no}`}
                              request={row.request}
                              priced={eventCosts?.priced ?? false}
                              maxCost={maxRowCost}
                              total={taskCostTotal}
                            />
                          )
                        }
                        const { line, pairedStartLineNo } = row.entry
                        if (row.request !== null) {
                          return <RequestDivider key={`request-${line.lineNo}`} request={row.request} priced={eventCosts?.priced ?? false} />
                        }
                        const itemCost = row.cost
                        const rawEventForKey = line.event && typeof line.event === 'object'
                          ? line.event as Record<string, unknown>
                          : null
                        const eventTypeForKey = rawEventForKey && typeof rawEventForKey.type === 'string'
                          ? rawEventForKey.type
                          : 'non-json'
                        const itemForKey = rawEventForKey && rawEventForKey.item && typeof rawEventForKey.item === 'object'
                          ? rawEventForKey.item as Record<string, unknown>
                          : null
                        const itemIdForKey = itemForKey && typeof itemForKey.id === 'string'
                          ? itemForKey.id
                          : ''
                        const timelineRowKey = [
                          'stdout-line',
                          String(line.lineNo),
                          line.capturedAtUtc || 'no-ts',
                          eventTypeForKey,
                          itemIdForKey,
                        ].join('|')
                        const event = line.event
                        if (!event) {
                          return (
                            <details key={timelineRowKey} className="agent-codex-event">
                              <summary className="agent-codex-event-summary">
                                <span className="agent-codex-event-line">#{line.lineNo}</span>
                                <Tag color="default">non-json</Tag>
                              </summary>
                              <pre className="agent-task-detail-pre agent-codex-event-pre">{line.raw}</pre>
                            </details>
                          )
                        }

                        const eventType = String(event.type || 'unknown')
                        const item = event.item as Record<string, unknown> | undefined
                        const itemType = item && typeof item.type === 'string' ? item.type : null
                        const itemId = item && typeof item.id === 'string' ? item.id : null
                        const status = item && typeof item.status === 'string' ? item.status : null
                        const exitCode = item && typeof item.exit_code === 'number' ? item.exit_code : null
                        const command = item && typeof item.command === 'string' ? item.command : null
                        const aggregatedOutput = item && typeof item.aggregated_output === 'string'
                          ? item.aggregated_output
                          : null
                        const isWebSearch =
                          itemType === 'webSearch' || itemType === 'web_search'
                        const webSearchAction = (
                          isWebSearch
                          && item
                          && item.action
                          && typeof item.action === 'object'
                        )
                          ? item.action as Record<string, unknown>
                          : null
                        const webSearchQueryFromItem = (
                          isWebSearch
                          && item
                          && typeof item.query === 'string'
                          && item.query.trim().length > 0
                        )
                          ? item.query.trim()
                          : null
                        const webSearchQueryFromAction = (
                          webSearchAction
                          && typeof webSearchAction.query === 'string'
                          && webSearchAction.query.trim().length > 0
                        )
                          ? webSearchAction.query.trim()
                          : null
                        const webSearchQueries = (
                          webSearchAction
                          && Array.isArray(webSearchAction.queries)
                        )
                          ? webSearchAction.queries
                            .filter((q): q is string => typeof q === 'string' && q.trim().length > 0)
                            .map((q) => q.trim())
                          : []
                        const webSearchQuery = webSearchQueryFromItem || webSearchQueryFromAction
                        const webSearchInlineQuery = webSearchQuery || webSearchQueries[0] || null
                        const contentItems = item && Array.isArray(item.content)
                          ? item.content as Array<Record<string, unknown>>
                          : []
                        const contentText = contentItems
                          .map((contentItem) => {
                            if (contentItem && typeof contentItem.text === 'string') {
                              return contentItem.text
                            }
                            return ''
                          })
                          .filter((part) => part.trim().length > 0)
                          .join('\n')
                        const reasoningText = item && typeof item.text === 'string' ? item.text : null
                        const messageText = item && typeof item.message === 'string' ? item.message : null
                        const itemText = reasoningText || messageText || contentText || null
                        const inlineItemText = itemText ? itemText.replace(/\s+/g, ' ').trim() : ''
                        const changes = item && Array.isArray(item.changes)
                          ? item.changes as Array<Record<string, unknown>>
                          : []
                        const fileChangePaths = itemType === 'file_change'
                          ? [
                            ...changes
                              .map((change) => (typeof change.path === 'string' ? change.path.trim() : ''))
                              .filter((path) => path.length > 0),
                            ...line.fileChangeDiffs
                              .map((diffItem) => diffItem.path.trim())
                              .filter((path) => path.length > 0),
                          ]
                          : []
                        const fileChangeNames = Array.from(
                          new Set(
                            fileChangePaths
                              .map((path) => basenameFromPath(path))
                              .filter((name) => name.length > 0)
                          )
                        )
                        const fileChangeCoverLabel = fileChangeNames.length === 0
                          ? null
                          : fileChangeNames.length === 1
                            ? fileChangeNames[0]
                            : `${fileChangeNames[0]} +${fileChangeNames.length - 1}`
                        const itemCoverLabel = itemType === 'file_change'
                          ? (fileChangeCoverLabel || itemId)
                          : itemId
                        const todoItems = item && Array.isArray(item.items)
                          ? item.items as Array<Record<string, unknown>>
                          : []
                        const usage = eventType === 'turn.completed' && event.usage && typeof event.usage === 'object'
                          ? event.usage as Record<string, unknown>
                          : null
                        const isCompletedCommandExecution =
                          eventType === 'item.completed' && itemType === 'command_execution'
                        const isCommandExecution =
                          itemType === 'command_execution' && typeof command === 'string' && command.trim().length > 0
                        const isAgentMessage =
                          (itemType === 'agent_message' || itemType === 'message' || itemType === 'assistant_message')
                          && typeof itemText === 'string'
                          && itemText.trim().length > 0
                        const isUserMessage =
                          itemType === 'user_message'
                          && eventType === 'item.completed'
                          && typeof itemText === 'string'
                          && itemText.trim().length > 0
                        const isReasoningStep =
                          itemType === 'reasoning' && inlineItemText.length > 0
                        const showItemText =
                          !!itemText
                          && !isReasoningStep
                          && (itemType !== 'user_message' || eventType === 'item.completed')
                        const openByDefault =
                          (
                            isAgentMessage
                            || isUserMessage
                            || isReasoningStep
                            || eventType === 'error'
                            || status === 'failed'
                          )
                          && !isCommandExecution
                        const lineLabel = pairedStartLineNo !== null
                          ? `#${pairedStartLineNo} -> #${line.lineNo}`
                          : `#${line.lineNo}`
                        const rawPayload = line.rawEvent ?? event
                        const eventClassNames = [
                          'agent-codex-event',
                          'agent-codex-event-frame',
                          isUserMessage ? 'agent-codex-event-user-message' : '',
                        ]
                          .filter(Boolean)
                          .join(' ')
                        const openRawEvent = () => {
                          let text = ''
                          try {
                            text = JSON.stringify(rawPayload, null, 2)
                          } catch {
                            text = String(rawPayload)
                          }
                          setRawEventModal({ lineNo: line.lineNo, eventType, text })
                        }

                        return (
                          <div key={timelineRowKey} className={eventClassNames}>
                            <details className="agent-codex-event-details" open={openByDefault}>
                              <summary className="agent-codex-event-summary">
                                <span className="agent-codex-event-line">{lineLabel}</span>
                                <Tag color={codexEventTagColor(eventType)}>{eventType}</Tag>
                                {itemType && <Tag color={codexItemTypeTagColor(itemType)}>{itemType}</Tag>}
                                {itemCoverLabel && <Tag>{itemCoverLabel}</Tag>}
                                {status && <Tag color={codexStatusTagColor(status)}>{status}</Tag>}
                                {exitCode !== null && <Tag color={exitCode === 0 ? 'green' : 'red'}>exit {exitCode}</Tag>}
                                {(line.itemDurationSeconds !== null || line.eventDurationSeconds !== null || line.totalDurationSeconds !== null) && (
                                  <Tag color="blue">
                                    <Sym s="⏱" meaning="Wall-clock time: this step | run so far" /> {formatDuration(line.itemDurationSeconds ?? line.eventDurationSeconds)} | {formatDuration(line.totalDurationSeconds)}
                                  </Tag>
                                )}
                                {isReasoningStep && (
                                  <span className="agent-codex-event-inline-text">
                                    {parseInlineMarkdown(inlineItemText)}
                                  </span>
                                )}
                                {itemCost !== null && (
                                  <CostChip
                                    value={eventCosts?.priced ? itemCost.total_usd : itemCost.tokens}
                                    priced={eventCosts?.priced ?? false}
                                    maxCost={maxRowCost}
                                    total={taskCostTotal}
                                  />
                                )}
                                {isCommandExecution && (
                                  <code className="agent-codex-event-command-inline">{command}</code>
                                )}
                                {isWebSearch && webSearchInlineQuery && (
                                  <code className="agent-codex-event-command-inline">
                                    web search: {webSearchInlineQuery}
                                  </code>
                                )}
                                {itemCost !== null && (
                                  <span className="agent-cost-split" title={APPROXIMATE_NOTE}>{costSplitText(itemCost, eventCosts?.priced ?? false)}</span>
                                )}
                              </summary>
                              {isCompletedCommandExecution && (
                                <div className="agent-codex-cmd-grid">
                                  <div className="agent-codex-event-row">
                                    <pre className="agent-task-detail-pre agent-codex-event-pre">{aggregatedOutput || '—'}</pre>
                                  </div>
                                </div>
                              )}
                              {!isCompletedCommandExecution && aggregatedOutput && (
                                <div className="agent-codex-event-row">
                                  <pre className="agent-task-detail-pre agent-codex-event-pre">{aggregatedOutput}</pre>
                                </div>
                              )}
                              {showItemText && (
                                <div className="agent-codex-event-row">
                                  <strong>{isUserMessage ? 'User Message' : isAgentMessage ? 'Message' : 'Text'}</strong>
                                  {(isAgentMessage || isUserMessage)
                                    ? renderMarkdownBlock(itemText)
                                    : <pre className="agent-task-detail-pre agent-codex-event-pre">{itemText}</pre>}
                                </div>
                              )}
                              {isWebSearch && (webSearchQuery || webSearchQueries.length > 0) && (
                                <div className="agent-codex-event-row">
                                  <strong>Web Search</strong>
                                  <div>
                                    {webSearchQuery && (
                                      <div>
                                        <Text code>query</Text>{' '}
                                        <Text code>{webSearchQuery}</Text>
                                      </div>
                                    )}
                                    {webSearchQueries.length > 0 && (
                                      <div>
                                        <Text code>queries</Text>
                                        {webSearchQueries.map((q, idx) => (
                                          <div key={`ws-query-${line.lineNo}-${idx}`}>
                                            <Text code>{q}</Text>
                                          </div>
                                        ))}
                                      </div>
                                    )}
                                  </div>
                                </div>
                              )}
                              {changes.length > 0 && (
                                <div className="agent-codex-event-row">
                                  <strong>File changes</strong>
                                  <div>
                                    {changes.map((change, idx) => {
                                      const changePath = String(change.path || '')
                                      const changeKindRaw = change.kind
                                      const changeKind = typeof changeKindRaw === 'string'
                                        ? changeKindRaw
                                        : (
                                          changeKindRaw
                                          && typeof changeKindRaw === 'object'
                                          && typeof (changeKindRaw as Record<string, unknown>).type === 'string'
                                        )
                                          ? String((changeKindRaw as Record<string, unknown>).type)
                                          : ''
                                      const movePath = (
                                        changeKindRaw
                                        && typeof changeKindRaw === 'object'
                                        && typeof (changeKindRaw as Record<string, unknown>).move_path === 'string'
                                      )
                                        ? String((changeKindRaw as Record<string, unknown>).move_path)
                                        : (
                                          changeKindRaw
                                          && typeof changeKindRaw === 'object'
                                          && typeof (changeKindRaw as Record<string, unknown>).movePath === 'string'
                                        )
                                          ? String((changeKindRaw as Record<string, unknown>).movePath)
                                          : ''
                                      const changeDiff = typeof change.diff === 'string' ? change.diff : null
                                      return (
                                        <div key={`change-${line.lineNo}-${idx}`}>
                                          <Text code>{changeKind || 'change'}</Text>{' '}
                                          <Text code>{changePath || '—'}</Text>
                                          {movePath && (
                                            <div>
                                              <Text code>move_to</Text>{' '}
                                              <Text code>{movePath}</Text>
                                            </div>
                                          )}
                                          {changeDiff && (
                                            <CodeBlock language="diff" text={changeDiff} theme="light" maxHeight="260px" />
                                          )}
                                        </div>
                                      )
                                    })}
                                  </div>
                                </div>
                              )}
                              {line.fileChangeDiffs.length > 0 && (
                                <div className="agent-codex-event-row">
                                  <strong>File diffs</strong>
                                  <div>
                                    {line.fileChangeDiffs.map((diffItem, idx) => (
                                      <details key={`diff-${line.lineNo}-${idx}`}>
                                        <summary>
                                          <Text code>{diffItem.kind || 'change'}</Text>{' '}
                                          <Text code>{diffItem.path || '—'}</Text>
                                        </summary>
                                        {diffItem.diffUnified
                                          ? (
                                            <CodeBlock language="diff" text={diffItem.diffUnified} theme="light" maxHeight="260px" />
                                          )
                                          : (
                                            <pre className="agent-task-detail-pre agent-codex-event-pre">
                                              (no textual diff)
                                            </pre>
                                          )}
                                      </details>
                                    ))}
                                  </div>
                                </div>
                              )}
                              {todoItems.length > 0 && (
                                <div className="agent-codex-event-row">
                                  <strong>Todo items</strong>
                                  <div>
                                    {todoItems.map((todo, idx) => (
                                      <div key={`todo-${line.lineNo}-${idx}`}>
                                        [{todo.completed ? 'x' : ' '}] {String(todo.text || '')}
                                      </div>
                                    ))}
                                  </div>
                                </div>
                              )}
                              {usage && (
                                <div className="agent-codex-event-row">
                                  <strong>Usage</strong>
                                  <div>
                                    input={String(usage.input_tokens ?? '0')}, output={String(usage.output_tokens ?? '0')}, cached={String(usage.cached_input_tokens ?? '0')}
                                  </div>
                                </div>
                              )}
                            </details>
                            <Button size="small" onClick={openRawEvent}>
                              Raw JSON
                            </Button>
                          </div>
                        )
                      })}
                    </div>
                  )}
                </div>
                <div><strong>stderr</strong></div>
                <div className="agent-task-detail-pre">{codexStderrSource || '—'}</div>
              </div>
            </div>

            <details
              onToggle={(evt) => {
                const target = evt.currentTarget as HTMLDetailsElement
                setShowRawTaskJson(target.open)
              }}
            >
              <summary>Raw Task JSON</summary>
              {showRawTaskJson && (
                <CodeBlock language="json" text={JSON.stringify(detailData.task, null, 2)} maxHeight="660px" />
              )}
            </details>
          </Space>
        )}
      </Modal>

      <Modal
        title={rawEventModal ? `Raw Event JSON #${rawEventModal.lineNo} (${rawEventModal.eventType})` : 'Raw Event JSON'}
        open={rawEventModal !== null}
        onCancel={() => setRawEventModal(null)}
        footer={(
          <Space>
            <Button
              onClick={async () => {
                const jsonText = rawEventModal?.text || ''
                if (!jsonText) return
                try {
                  await copyTextToClipboard(jsonText)
                  message.success('Copied raw event JSON')
                } catch (err) {
                  console.error(err)
                  message.error('Failed to copy raw event JSON')
                }
              }}
              disabled={!rawEventModal?.text}
            >
              Copy JSON
            </Button>
            <Button onClick={() => setRawEventModal(null)}>
              Close
            </Button>
          </Space>
        )}
        width="80vw"
        style={{ top: 24 }}
      >
        <CodeBlock language="json" text={rawEventModal?.text || '—'} maxHeight="70vh" />
      </Modal>
    </>
  )
}
