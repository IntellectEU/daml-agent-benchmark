// The mutation catalogue: every seeded bug of the test-generation tasks, its patch, and the
// original test file's scripts it breaks. A summary strip filters by origin and kind, the
// left column picks a task, and the right column shows all of that task's mutations, each
// as a side-by-side diff that can widen to its whole file.

import { createContext, Fragment, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { Button, Card, Input, Spin, Tag, Tooltip, Typography } from 'antd'
import type { HighlighterCore } from 'shiki/core'

import { fetchMutationCatalogue, fetchTaskMutations } from '../api'
import { intralineSegments, markRuns, parseUnifiedDiff, wholeFileRows } from '../diffView'
import type { DiffHunk, DiffRow, Segment } from '../diffView'
import { readErrorDetail } from '../format'
import { highlightLines, useDamlHighlighter } from '../highlighter'
import type { CodeRun } from '../highlighter'
import type {
  CatalogueMutation,
  MutationCatalogueTask,
  MutationSource,
  MutationSummary,
  MutationValidationOutcome,
  TaskMutationsResponse,
} from '../types'

const { Text, Title } = Typography

// The Daml highlighter once it has loaded; the catalogue starts the load when it opens.
const HighlighterContext = createContext<HighlighterCore | null>(null)

type Filters = { kind: string | null; source: MutationSource | null }

function matches(m: MutationSummary, filters: Filters): boolean {
  return (filters.kind === null || m.kind === filters.kind) && (filters.source === null || m.source === filters.source)
}

const OUTCOME_TAG: Record<MutationValidationOutcome, { label: string; color: string }> = {
  killed: { label: 'caught by the original tests', color: 'green' },
  survived: { label: 'missed by the original tests', color: 'gold' },
  'does-not-compile': { label: 'does not compile', color: 'red' },
  'does-not-apply': { label: 'does not apply', color: 'red' },
  'not-impl-only': { label: 'patches a non-implementation file', color: 'red' },
}

function plural(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? '' : 's'}`
}

// The script name without the path grading keys it by: `daml/Test.daml:testCancel` -> `testCancel`.
function scriptName(key: string): string {
  return key.slice(key.lastIndexOf(':') + 1)
}

// Text with its `backticked` spans set as code, as the mutation files write them.
function InlineCode({ text }: { text: string }) {
  return (
    <>
      {text.split('`').map((part, i) => (i % 2 === 1 ? <Text key={i} code>{part}</Text> : <Fragment key={i}>{part}</Fragment>))}
    </>
  )
}

// A run's colour and font style as CSS; a run without a colour is plain text.
function runStyle(run: CodeRun): CSSProperties | undefined {
  if (run.color === undefined) return undefined
  const bits = run.fontStyle ?? 0
  return {
    color: run.color,
    fontStyle: bits & 1 ? 'italic' : undefined,
    fontWeight: bits & 2 ? 600 : undefined,
    textDecoration: bits & 4 ? 'underline' : undefined,
  }
}

function Code({ runs }: { runs: CodeRun[] }) {
  return (
    <>
      {runs.map((run, i) => (
        <span key={i} style={runStyle(run)}>{run.text}</span>
      ))}
    </>
  )
}

function ChangedCode({ runs, segments }: { runs: CodeRun[]; segments: Segment[] }) {
  return (
    <>
      {markRuns(runs, segments).map((s, i) => (s.changed ? <span key={i} className="mc-x"><Code runs={s.runs} /></span> : <Code key={i} runs={s.runs} />))}
    </>
  )
}

// One side of the rows as highlighted lines, null where the side has no line.
function sideRuns(highlighter: HighlighterCore | null, texts: Array<string | null>): Array<CodeRun[] | null> {
  const lines = highlightLines(highlighter, texts.filter((t) => t !== null))
  let next = 0
  return texts.map((t) => (t === null ? null : lines[next++]))
}

function DiffGrid({ rows }: { rows: DiffRow[] }) {
  const highlighter = useContext(HighlighterContext)
  const oldRuns = useMemo(() => sideRuns(highlighter, rows.map((r) => r.oldText)), [highlighter, rows])
  const newRuns = useMemo(() => sideRuns(highlighter, rows.map((r) => r.newText)), [highlighter, rows])
  return (
    <div className="mc-sbs">
      {rows.map((row, i) => {
        const oldClass = row.oldText === null ? 'mc-gap' : row.changed ? 'mc-del' : ''
        const newClass = row.newText === null ? 'mc-gap' : row.changed ? 'mc-add' : ''
        const pair = row.changed && row.oldText !== null && row.newText !== null ? intralineSegments(row.oldText, row.newText) : null
        const oldLine = oldRuns[i]
        const newLine = newRuns[i]
        return (
          <Fragment key={i}>
            <span className={`mc-ln ${oldClass}`}>{row.oldNo ?? ''}</span>
            <span className={`mc-old ${oldClass}`}>{oldLine && (pair ? <ChangedCode runs={oldLine} segments={pair[0]} /> : <Code runs={oldLine} />)}</span>
            <span className={`mc-ln ${newClass}`}>{row.newNo ?? ''}</span>
            <span className={newClass}>{newLine && (pair ? <ChangedCode runs={newLine} segments={pair[1]} /> : <Code runs={newLine} />)}</span>
          </Fragment>
        )
      })}
    </div>
  )
}

// One patched file: its hunks, or the whole file with the hunks in place. The toggle keeps
// the file's header where it was on the screen, so the code under the pointer stays put.
function FileDiff({ file, hunks, original }: { file: string; hunks: DiffHunk[]; original: string | undefined }) {
  const [whole, setWhole] = useState(false)
  const headRef = useRef<HTMLDivElement | null>(null)
  const topBefore = useRef<number | null>(null)
  const rows = useMemo(() => (whole && original !== undefined ? wholeFileRows(hunks, original) : null), [hunks, original, whole])
  useLayoutEffect(() => {
    if (topBefore.current === null || headRef.current === null) return
    window.scrollBy(0, headRef.current.getBoundingClientRect().top - topBefore.current)
    topBefore.current = null
  }, [whole])
  const toggle = () => {
    topBefore.current = headRef.current?.getBoundingClientRect().top ?? null
    setWhole((w) => !w)
  }
  return (
    <>
      <div className="mc-file-line" ref={headRef}>
        <span>{file}</span>
        {original !== undefined && (
          <Button size="small" type={whole ? 'primary' : 'default'} ghost={whole} onClick={toggle}>
            {whole ? 'Changes only' : 'Whole file'}
          </Button>
        )}
      </div>
      {rows ? (
        <DiffGrid rows={rows} />
      ) : (
        hunks.map((hunk, i) => (
          <Fragment key={i}>
            <div className="mc-hunk">{hunk.context || ' '}</div>
            <DiffGrid rows={hunk.rows} />
          </Fragment>
        ))
      )}
    </>
  )
}

function MutationCard({ mutation, files }: { mutation: CatalogueMutation; files: Record<string, string> }) {
  const hunks = useMemo(() => parseUnifiedDiff(mutation.patch), [mutation.patch])
  const paths = [...new Set(hunks.map((h) => h.file))]
  const validation = mutation.validation
  const ran = validation !== null && (validation.outcome === 'killed' || validation.outcome === 'survived')
  const outcome = validation === null ? null : OUTCOME_TAG[validation.outcome]
  return (
    <details className="mc-mut" open>
      <summary>
        <span className="mc-mut-id">{mutation.id}</span>
        <span className="mc-tags">
          <Tag bordered={false}>{mutation.kind}</Tag>
          {mutation.source === 'real-bug' && <Tag bordered={false} color="orange">real bug</Tag>}
          {outcome && (
            <Tooltip title={validation?.outcome_detail}>
              <Tag bordered={false} color={outcome.color}>{outcome.label}</Tag>
            </Tooltip>
          )}
        </span>
        <span className="mc-mut-reason"><InlineCode text={mutation.reason} /></span>
      </summary>
      <div className="mc-detail">
        {(mutation.target_script || mutation.commit || ran) && (
          <dl className="mc-meta">
            {mutation.target_script && (
              <>
                <dt>Aimed at</dt>
                <dd>{mutation.target_script}</dd>
              </>
            )}
            {mutation.commit && (
              <>
                <dt>Undoes fix</dt>
                <dd>{mutation.commit}</dd>
              </>
            )}
            {ran && (
              <>
                <dt>Breaks</dt>
                <dd>{validation.newly_failing.length ? validation.newly_failing.map(scriptName).join(', ') : 'no script of the original test file'}</dd>
              </>
            )}
          </dl>
        )}
        <div className="mc-diff" role="region" aria-label={`Patch of ${mutation.id}`} tabIndex={0}>
          {paths.map((path) => (
            <FileDiff key={path} file={path} hunks={hunks.filter((h) => h.file === path)} original={files[path]} />
          ))}
        </div>
      </div>
    </details>
  )
}

function TaskMutations({
  task,
  detail,
  error,
  filters,
}: {
  task: MutationCatalogueTask
  detail: TaskMutationsResponse | undefined
  error: string | undefined
  filters: Filters
}) {
  const shown = task.mutations.filter((m) => matches(m, filters))
  const real = task.mutations.filter((m) => m.source === 'real-bug').length
  const filtered = filters.kind !== null || filters.source !== null
  return (
    <section className="mc-task" aria-live="polite">
      <div className="mc-col-head">
        <h2>Mutations of this task</h2>
        <span className="mc-n">{shown.length}</span>
      </div>
      <Card size="small">
        <div className="mc-task-head">
          <span className="mc-repo">{task.repo}</span>
          <span className="mc-task-short">{task.short_name}</span>
          <span className="mc-task-path">{task.task_id.slice(task.repo.length + 1)}</span>
          <Text type="secondary">
            {plural(task.mutations.length, 'mutation')}{real ? `, ${real} of them ${real === 1 ? 'a real bug' : 'real bugs'}` : ''}
            {detail && detail.scripts !== null ? ` · the original test file has ${plural(detail.scripts, 'script')}` : ''}
            {filtered ? ` · showing ${shown.length} that ${shown.length === 1 ? 'matches' : 'match'} the filters` : ''}
          </Text>
        </div>
      </Card>
      {error !== undefined ? (
        <div className="agent-task-detail-error">{error}</div>
      ) : detail === undefined ? (
        <div className="mc-empty"><Spin /></div>
      ) : (
        detail.mutations.filter((m) => matches(m, filters)).map((m) => <MutationCard key={m.id} mutation={m} files={detail.files} />)
      )}
    </section>
  )
}

function Chip({ checked, real, onClick, children }: { checked: boolean; real?: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <Tag.CheckableTag checked={checked} onChange={onClick} className={`mc-chip${real ? ' mc-chip-real' : ''}`}>
      {children}
    </Tag.CheckableTag>
  )
}

export function MutationCatalogue({
  selectedFileName,
  onSelect,
  onBack,
}: {
  selectedFileName: string | null
  onSelect: (fileName: string) => void
  onBack: () => void
}) {
  const [tasks, setTasks] = useState<MutationCatalogueTask[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [details, setDetails] = useState<Record<string, TaskMutationsResponse>>({})
  // A task whose mutations failed to load shows why, and is not asked for again.
  const [detailErrors, setDetailErrors] = useState<Record<string, string>>({})
  const [filters, setFilters] = useState<Filters>({ kind: null, source: null })
  const [query, setQuery] = useState('')
  const highlighter = useDamlHighlighter()

  useEffect(() => {
    fetchMutationCatalogue().then(
      (res) => setTasks(res.tasks),
      (err) => setListError(`Failed to load the mutation catalogue: ${readErrorDetail(err, 'no detail')}`)
    )
  }, [])

  const all = useMemo(() => (tasks ?? []).flatMap((t) => t.mutations), [tasks])
  const realBugs = all.filter((m) => m.source === 'real-bug').length
  // The kind chips count over the origin chosen, most common first.
  const kindCounts = useMemo(() => {
    const counts = new Map<string, number>()
    for (const m of all) if (filters.source === null || m.source === filters.source) counts.set(m.kind, (counts.get(m.kind) ?? 0) + 1)
    return [...counts].sort((a, b) => b[1] - a[1])
  }, [all, filters.source])

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase()
    return (tasks ?? []).filter(
      (t) =>
        t.mutations.some((m) => matches(m, filters)) &&
        (!q || t.task_id.toLowerCase().includes(q) || t.mutations.some((m) => m.id.toLowerCase().includes(q)))
    )
  }, [filters, query, tasks])
  // The task asked for, or the first one shown when the filters hide it.
  const selected = visible.find((t) => t.file_name === selectedFileName) ?? visible[0]

  useEffect(() => {
    if (selected === undefined || details[selected.file_name] !== undefined || detailErrors[selected.file_name] !== undefined) return
    fetchTaskMutations(selected.file_name).then(
      (res) => setDetails((prev) => ({ ...prev, [selected.file_name]: res })),
      (err) =>
        setDetailErrors((prev) => ({
          ...prev,
          [selected.file_name]: `Failed to load the mutations of ${selected.task_id}: ${readErrorDetail(err, 'no detail')}`,
        }))
    )
  }, [detailErrors, details, selected])

  const setSource = (source: MutationSource | null) => {
    // A kind the new origin has no mutation of would hide everything.
    const kindLeft = filters.kind !== null && all.some((m) => m.kind === filters.kind && (source === null || m.source === source))
    setFilters({ source, kind: kindLeft ? filters.kind : null })
  }

  return (
    <HighlighterContext value={highlighter}>
      <div className="mc-wrap">
        <div className="mc-header">
          <Button onClick={onBack}>← Runs</Button>
          <Title level={4} style={{ margin: 0 }}>Mutation catalogue</Title>
          <Text type="secondary">Test-generation tasks · every seeded bug, its patch, and the original test file's scripts it breaks</Text>
        </div>
  
        <Card size="small">
          <div className="mc-summary">
            <span className="mc-stat"><b>{tasks?.length ?? 0}</b>tasks</span>
            <span className="mc-stat"><b>{all.length}</b>mutations</span>
            <div className="mc-chips mc-sources" role="group" aria-label="Filter by origin">
              <Chip checked={filters.source === null} onClick={() => setSource(null)}>any origin</Chip>
              <Chip checked={filters.source === 'llm'} onClick={() => setSource('llm')}>model-written {all.length - realBugs}</Chip>
              <Chip checked={filters.source === 'real-bug'} real onClick={() => setSource('real-bug')}>real bugs {realBugs}</Chip>
            </div>
            <div className="mc-chips mc-kinds" role="group" aria-label="Filter by kind">
              <Chip checked={filters.kind === null} onClick={() => setFilters({ ...filters, kind: null })}>all kinds</Chip>
              {kindCounts.map(([kind, n]) => (
                <Chip key={kind} checked={filters.kind === kind} onClick={() => setFilters({ ...filters, kind })}>{kind} {n}</Chip>
              ))}
            </div>
          </div>
        </Card>
  
        {listError !== null ? (
          <div className="agent-task-detail-error">{listError}</div>
        ) : tasks === null ? (
          <div className="mc-empty"><Spin /></div>
        ) : (
          <div className="mc-main">
            <nav className="mc-index" aria-label="Tasks">
              <div className="mc-col-head">
                <h2>Tasks</h2>
                <span className="mc-n">{visible.length === tasks.length ? visible.length : `${visible.length} of ${tasks.length}`}</span>
              </div>
              <Input
                variant="borderless"
                allowClear
                placeholder="Filter tasks or mutation ids"
                aria-label="Filter tasks or mutation ids"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                className="mc-search"
              />
              <ul>
                {visible.map((t) => (
                  <li key={t.file_name}>
                    <Tooltip title={t.task_id} placement="right" mouseEnterDelay={0.4}>
                      <button type="button" aria-current={t === selected} onClick={() => onSelect(t.file_name)}>
                        <span className="mc-repo">{t.repo}</span>
                        <span className="mc-short">{t.short_name}</span>
                        <span className="mc-count">{t.mutations.filter((m) => matches(m, filters)).length}</span>
                      </button>
                    </Tooltip>
                  </li>
                ))}
                {visible.length === 0 && <li className="mc-empty">No task matches.</li>}
              </ul>
            </nav>
            {selected === undefined ? (
              <div className="mc-empty">No task matches.</div>
            ) : (
              <TaskMutations task={selected} detail={details[selected.file_name]} error={detailErrors[selected.file_name]} filters={filters} />
            )}
          </div>
        )}
        <Text type="secondary" className="mc-note">
          Each mutation's outcome is its last validation against the original test file, where this machine has a report.
          A real bug the original tests miss is kept on purpose: it measures whether an agent's tests do better.
        </Text>
      </div>
    </HighlighterContext>
  )
}
