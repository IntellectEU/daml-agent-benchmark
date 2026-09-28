// Display text for the figures and errors the dashboard shows. Durations, costs and
// token counts get a compact form; an error gets one line with what the server said.

import axios from 'axios'

import type { AgentTaskCell } from './types'

export function formatDuration(seconds: number | null): string {
  if (seconds === null) return '—'
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  if (seconds < 3600) return `${(seconds / 60).toFixed(1)}m`
  return `${(seconds / 3600).toFixed(2)}h`
}

// How many of the task's test scripts passed, or null when the task has none to count.
export function formatTestTally(cell: AgentTaskCell): string | null {
  return cell.tests_total ? `${cell.tests_succeeded} of ${cell.tests_total} test scripts passed` : null
}

export function formatUsd(usd: number | null, isLowerBound = false): string {
  if (usd === null) return '—'
  const prefix = isLowerBound ? '>' : ''
  return `${prefix}$${usd.toFixed(2)}`
}

// A cost small enough to need four decimals, as the event timeline shows it.
export function formatSmallUsd(usd: number | null): string {
  if (usd === null) return '—'
  if (usd > 0 && usd < 0.0001) return '<$0.0001'
  return `$${usd >= 0.01 ? usd.toFixed(3) : usd.toFixed(4)}`
}

export function formatTokenCount(tokens: number | null, isLowerBound = false): string {
  if (tokens === null) return '—'
  const prefix = isLowerBound ? '>' : ''
  return `${prefix}${Math.round(tokens).toLocaleString()}`
}

// A token count in millions, as the run table shows it.
export function formatMillionTokens(tokens: number, isLowerBound = false): string {
  const prefix = isLowerBound ? '>' : ''
  if (tokens === 0) return `${prefix}0`
  return `${prefix}${(tokens / 1e6).toFixed(tokens >= 1e7 ? 1 : 2)}M`
}

export function readErrorDetail(err: unknown, fallback: string): string {
  if (axios.isAxiosError(err)) {
    const responseData: unknown = err.response?.data
    const responseText =
      typeof responseData === 'string'
        ? responseData
        : responseData && typeof responseData === 'object'
          ? JSON.stringify(responseData)
          : ''
    return [err.message, err.code, responseText].filter(Boolean).join(' | ') || fallback
  }
  if (err instanceof Error) return err.message || fallback
  return fallback
}
