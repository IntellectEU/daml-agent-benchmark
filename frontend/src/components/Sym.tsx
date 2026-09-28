// A symbol with its meaning shown when the pointer rests on it.

import { Tooltip } from 'antd'

import { SYMBOL_MEANING } from '../symbols'

export function Sym({ s, meaning }: { s: string; meaning?: string }) {
  return (
    <Tooltip title={meaning ?? SYMBOL_MEANING[s] ?? s} mouseEnterDelay={0.15} mouseLeaveDelay={0}>
      <span>{s}</span>
    </Tooltip>
  )
}
