import { useCallback, useEffect, useState } from 'react'

type RunLike = {
  run_id: string
}

function loadPins(storageKey: string): Set<string> {
  try {
    const raw = window.localStorage.getItem(storageKey)
    if (!raw) return new Set<string>()
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return new Set<string>()
    return new Set(parsed.map(String))
  } catch {
    return new Set<string>()
  }
}

function persistPins(storageKey: string, pins: Set<string>): void {
  window.localStorage.setItem(storageKey, JSON.stringify(Array.from(pins)))
}

export function useRunPins(storageKey: string) {
  const [pins, setPins] = useState<Set<string>>(() => loadPins(storageKey))

  useEffect(() => {
    persistPins(storageKey, pins)
  }, [pins, storageKey])

  const togglePin = useCallback((runId: string): void => {
    setPins((prev) => {
      const next = new Set(prev)
      if (next.has(runId)) next.delete(runId)
      else next.add(runId)
      return next
    })
  }, [])

  const isPinned = useCallback((runId: string): boolean => pins.has(runId), [pins])

  return { pins, togglePin, isPinned }
}

export function orderPinnedRuns<T extends RunLike>(rows: T[], pins: Set<string>): T[] {
  const pinnedRuns: T[] = []
  const unpinnedRuns: T[] = []
  for (const row of rows) {
    if (pins.has(row.run_id)) pinnedRuns.push(row)
    else unpinnedRuns.push(row)
  }
  return [...pinnedRuns, ...unpinnedRuns]
}
