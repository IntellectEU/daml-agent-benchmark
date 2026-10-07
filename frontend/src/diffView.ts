// A unified diff as side-by-side rows, the words that differ within a changed line, and
// those words laid over a highlighted line. The mutation catalogue draws its patches with
// these. Nothing here touches the DOM or the highlighter, so `tests/diffView.test.mjs` runs
// it under Node (`npm test`).

// One row of the side-by-side view. A side is null where the other side has a line it lacks.
export type DiffRow = {
  oldNo: number | null
  oldText: string | null
  newNo: number | null
  newText: string | null
  changed: boolean
}

export type DiffHunk = {
  file: string
  // What git prints after the hunk's line numbers: the enclosing definition, often.
  context: string
  oldStart: number
  newStart: number
  rows: DiffRow[]
}

// The hunks of a unified diff, in order. Removed and added lines that follow each other
// pair up row by row; the longer run leaves the other side empty.
export function parseUnifiedDiff(patch: string): DiffHunk[] {
  const hunks: DiffHunk[] = []
  let file = ''
  let rows: DiffRow[] | null = null
  let oldNo = 0
  let newNo = 0
  // Lines of the current hunk not read yet, from its header. Inside a hunk every line is
  // content, so a removed `-- comment` line, which reads `--- comment`, is not a file header.
  let oldLeft = 0
  let newLeft = 0
  let dels: Array<[number, string]> = []
  let adds: Array<[number, string]> = []
  const flush = () => {
    for (let j = 0; j < Math.max(dels.length, adds.length); j++) {
      rows!.push({
        oldNo: dels[j]?.[0] ?? null,
        oldText: dels[j]?.[1] ?? null,
        newNo: adds[j]?.[0] ?? null,
        newText: adds[j]?.[1] ?? null,
        changed: true,
      })
    }
    dels = []
    adds = []
  }
  for (const line of patch.replace(/\n$/, '').split('\n')) {
    if (rows && (oldLeft > 0 || newLeft > 0) && !line.startsWith('\\')) {
      if (line.startsWith('-')) {
        dels.push([oldNo++, line.slice(1)])
        oldLeft--
      } else if (line.startsWith('+')) {
        adds.push([newNo++, line.slice(1)])
        newLeft--
      } else {
        flush()
        const text = line.slice(1)
        rows.push({ oldNo: oldNo++, oldText: text, newNo: newNo++, newText: text, changed: false })
        oldLeft--
        newLeft--
      }
      continue
    }
    if (line.startsWith('+++ ')) {
      file = line.slice(4).replace(/^b\//, '')
      continue
    }
    if (line.startsWith('--- ') || line.startsWith('diff --git') || line.startsWith('index ')) continue
    const header = line.match(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)/)
    if (header) {
      if (rows) flush()
      oldNo = Number(header[1])
      newNo = Number(header[3])
      oldLeft = header[2] === undefined ? 1 : Number(header[2])
      newLeft = header[4] === undefined ? 1 : Number(header[4])
      rows = []
      hunks.push({ file, context: header[5].trim(), oldStart: oldNo, newStart: newNo, rows })
      continue
    }
    // Anything else between hunks, such as git's "\ No newline at end of file", is not content.
  }
  if (rows) flush()
  return hunks
}

// One file's hunks with every unchanged line of the original file around them.
export function wholeFileRows(hunks: DiffHunk[], original: string): DiffRow[] {
  const lines = original.replace(/\n$/, '').split('\n')
  const rows: DiffRow[] = []
  const unchanged = (no: number, offset: number): DiffRow => ({
    oldNo: no,
    oldText: lines[no - 1],
    newNo: no + offset,
    newText: lines[no - 1],
    changed: false,
  })
  let next = 1
  let offset = 0
  for (const hunk of hunks) {
    for (; next < hunk.oldStart; next++) rows.push(unchanged(next, offset))
    rows.push(...hunk.rows)
    next = hunk.oldStart + hunk.rows.filter((r) => r.oldText !== null).length
    offset = hunk.newStart + hunk.rows.filter((r) => r.newText !== null).length - next
  }
  for (; next <= lines.length; next++) rows.push(unchanged(next, offset))
  return rows
}

// A run of a line's text, and whether it differs from the other side.
export type Segment = { text: string; changed: boolean }

// The two lines of a changed pair cut into runs that the other line shares or not. A
// word-level longest common subsequence decides what is shared.
export function intralineSegments(a: string, b: string): [Segment[], Segment[]] {
  const tokens = (s: string) => s.match(/[A-Za-z0-9_']+|\s+|[^A-Za-z0-9_'\s]/g) ?? []
  const x = tokens(a)
  const y = tokens(b)
  const lcs = Array.from({ length: x.length + 1 }, () => new Array<number>(y.length + 1).fill(0))
  for (let i = x.length - 1; i >= 0; i--) {
    for (let j = y.length - 1; j >= 0; j--) {
      lcs[i][j] = x[i] === y[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1])
    }
  }
  const sx: Segment[] = []
  const sy: Segment[] = []
  const push = (segments: Segment[], text: string, changed: boolean) => {
    const last = segments[segments.length - 1]
    if (last && last.changed === changed) last.text += text
    else segments.push({ text, changed })
  }
  let i = 0
  let j = 0
  while (i < x.length && j < y.length) {
    if (x[i] === y[j]) {
      push(sx, x[i++], false)
      push(sy, y[j++], false)
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) push(sx, x[i++], true)
    else push(sy, y[j++], true)
  }
  while (i < x.length) push(sx, x[i++], true)
  while (j < y.length) push(sy, y[j++], true)
  return [sx, sy]
}

// A line's highlighted runs cut where its segments start and end, so a run that a segment
// edge crosses becomes two pieces that keep its colours. Each segment holds the pieces it
// covers; a segment of only whitespace does not count as changed. The runs and the
// segments spell the same line.
export function markRuns<T extends { text: string }>(runs: T[], segments: Segment[]): Array<{ changed: boolean; runs: T[] }> {
  const marked: Array<{ changed: boolean; runs: T[] }> = []
  let i = 0
  let offset = 0
  for (const segment of segments) {
    const pieces: T[] = []
    let left = segment.text.length
    while (left > 0) {
      const run = runs[i]
      const take = Math.min(left, run.text.length - offset)
      pieces.push({ ...run, text: run.text.slice(offset, offset + take) })
      left -= take
      offset += take
      if (offset === run.text.length) {
        i++
        offset = 0
      }
    }
    marked.push({ changed: segment.changed && segment.text.trim() !== '', runs: pieces })
  }
  return marked
}
