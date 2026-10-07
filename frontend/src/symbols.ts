// The symbols the dashboard shows for task statuses and run figures, and what each
// stands for. The matrix and the task detail both draw on them.

import type { AgentTaskCell, AgentTaskStatusCode } from './types'

export const STATUS_META: Record<AgentTaskStatusCode, { emoji: string; label: string }> = {
  queued: { emoji: '🕓', label: 'Queued: waiting for a worker' },
  running: { emoji: '⏳', label: 'Running' },
  success: { emoji: '✅', label: 'Success' },
  usage_limit: { emoji: '🪫', label: 'Usage limit reached' },
  tests_failed: { emoji: '🧪', label: 'Tests failed' },
  build_failed: { emoji: '🛠', label: 'Build/syntax failed' },
  parse_failed: { emoji: '📄', label: 'Parse failed' },
  infra_failed: { emoji: '⚠️', label: 'Infra failure' },
  security_violation: { emoji: '🚫', label: 'Security violation' },
  other_error: { emoji: '❌', label: 'Other failure' },
}

// What a symbol stands for, shown when the pointer rests on it.
export const SYMBOL_MEANING: Record<string, string> = {
  '⏱': 'Wall-clock time',
  '⌛': 'Timed out',
  '⚪': 'Not run',
}

export function statusEmoji(cell: AgentTaskCell | undefined): string {
  if (!cell) return '⚪'
  const base = STATUS_META[cell.status_code].emoji
  if (cell.timed_out) return `${base}⌛`
  return base
}

// The run table's per-run figures: the symbol heading each column and what it counts.
export const RUN_COLUMN_META = {
  tests: { emoji: '📋', label: 'Test scripts passed, over all tasks' },
  mutants: { emoji: '🐞', label: 'Mutants caught by test files that pass on the correct code, over all mutants' },
  duration: { emoji: '⏱', label: 'Duration: wall-clock time of the run' },
  cost: { emoji: '💵', label: 'Cost of the run in USD. A leading > means part of the usage went unrecorded.' },
  fresh: { emoji: '👓', label: 'Fresh input tokens' },
  cached: { emoji: '💾', label: 'Cached input tokens' },
  output: { emoji: '✍️', label: 'Output tokens' },
} as const

// What a run-table outcome column counts. A column of successes reads as "passed".
export function statusColumnLabel(status: AgentTaskStatusCode): string {
  if (status === 'success') return 'Passed: tasks whose tests all passed'
  return STATUS_META[status].label
}
