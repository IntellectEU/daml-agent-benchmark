import type { ReactNode } from 'react'

import { Button, Space, Tag, Tooltip, Typography } from 'antd'

const { Text } = Typography

type RunNameCellTag = {
  key: string
  label: ReactNode
  color?: string
}

type RunNameCellProps = {
  runId: string
  runName: string
  copiedRunId: string | null
  onCopyRunId: (runId: string) => void | Promise<void>
  tags?: RunNameCellTag[]
}

export function RunNameCell({ runId, runName, copiedRunId, onCopyRunId, tags = [] }: RunNameCellProps) {
  const visibleTags = tags.filter((tag) => Boolean(tag.label))

  const header = (
    <Space size={6} align="center">
      <Text>{runName}</Text>
      <Tooltip title={runId} mouseEnterDelay={0} mouseLeaveDelay={0}>
        <Button
          size="small"
          aria-label={copiedRunId === runId ? 'Run ID copied' : `Copy run ID ${runId}`}
          onClick={() => void onCopyRunId(runId)}
        >
          {copiedRunId === runId ? '✅' : '📋'}
        </Button>
      </Tooltip>
    </Space>
  )

  if (visibleTags.length === 0) {
    return header
  }

  return (
    <Space direction="vertical" size={2}>
      {header}
      <Space size={4} wrap>
        {visibleTags.map((tag) => (
          <Tag key={tag.key} color={tag.color}>
            {tag.label}
          </Tag>
        ))}
      </Space>
    </Space>
  )
}
