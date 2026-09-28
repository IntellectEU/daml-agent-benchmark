import { useCallback, useEffect, useRef, useState } from 'react'

import { copyTextToClipboard } from './clipboard'

export function useClipboardFeedback(resetMs = 1200): {
  copiedValue: string | null
  copyWithFeedback: (value: string) => Promise<boolean>
} {
  const [copiedValue, setCopiedValue] = useState<string | null>(null)
  const timerRef = useRef<number | null>(null)

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current)
      }
    }
  }, [])

  const copyWithFeedback = useCallback(async (text: string): Promise<boolean> => {
    if (!text) return false

    try {
      await copyTextToClipboard(text)
      setCopiedValue(text)
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current)
      }
      timerRef.current = window.setTimeout(() => {
        setCopiedValue((current) => (current === text ? null : current))
        timerRef.current = null
      }, resetMs)
      return true
    } catch {
      return false
    }
  }, [resetMs])

  return {
    copiedValue,
    copyWithFeedback,
  }
}
