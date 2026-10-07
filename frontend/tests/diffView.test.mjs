// Unit tests of the mutation catalogue's diff view, on small fixed patches.
//
//     npm test

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { intralineSegments, markRuns, parseUnifiedDiff, wholeFileRows } from '../src/diffView.ts'

const ORIGINAL = ['module M where', '', 'f x = x + 1', 'g y = y * 2', 'h = 3', '', 'k = 4', 'm = 5', ''].join('\n')

// Two hunks in one file: a replaced line, then a removed one.
const TWO_HUNKS = `diff --git a/M.daml b/M.daml
--- a/M.daml
+++ b/M.daml
@@ -2,3 +2,3 @@ module M where

-f x = x + 1
+f x = x - 1
 g y = y * 2
@@ -6,3 +6,2 @@ h = 3

-k = 4
 m = 5
`

const sides = (rows) => ({
  oldNos: rows.filter((r) => r.oldText !== null).map((r) => r.oldNo),
  newNos: rows.filter((r) => r.newText !== null).map((r) => r.newNo),
  oldText: rows.filter((r) => r.oldText !== null).map((r) => r.oldText).join('\n'),
  newText: rows.filter((r) => r.newText !== null).map((r) => r.newText).join('\n'),
})
const oneToN = (nos) => nos.every((no, i) => no === i + 1)

test('a replaced line sits next to its replacement, and a removed one next to a gap', () => {
  const hunks = parseUnifiedDiff(TWO_HUNKS)
  assert.equal(hunks.length, 2)
  assert.deepEqual(hunks.map((h) => [h.file, h.context, h.oldStart, h.newStart]), [
    ['M.daml', 'module M where', 2, 2],
    ['M.daml', 'h = 3', 6, 6],
  ])
  const replaced = hunks[0].rows.find((r) => r.changed)
  assert.deepEqual([replaced.oldText, replaced.newText], ['f x = x + 1', 'f x = x - 1'])
  const removed = hunks[1].rows.find((r) => r.changed)
  assert.deepEqual([removed.oldNo, removed.oldText, removed.newText], [7, 'k = 4', null])
})

test('the whole file rebuilds the original and the patched file, numbered without gaps', () => {
  const rows = wholeFileRows(parseUnifiedDiff(TWO_HUNKS), ORIGINAL)
  const { oldNos, newNos, oldText, newText } = sides(rows)
  assert.ok(oneToN(oldNos) && oneToN(newNos))
  assert.equal(oldText, ORIGINAL.replace(/\n$/, ''))
  assert.equal(newText, ORIGINAL.replace('x + 1', 'x - 1').replace('k = 4\n', '').replace(/\n$/, ''))
})

test('a removed comment at column 0 is content, not a file header', () => {
  const patch = `--- a/C.daml
+++ b/C.daml
@@ -1,3 +1,2 @@
 module C where
--- the answer
 answer = 42
`
  const [hunk] = parseUnifiedDiff(patch)
  assert.equal(hunk.file, 'C.daml')
  assert.deepEqual(hunk.rows.map((r) => [r.oldText, r.newText]), [
    ['module C where', 'module C where'],
    ['-- the answer', null],
    ['answer = 42', 'answer = 42'],
  ])
})

test('the no-newline marker is not a line, and each file of a patch keeps its own hunks', () => {
  const patch = `--- a/A.daml
+++ b/A.daml
@@ -1 +1 @@
-a = 1
\\ No newline at end of file
+a = 2
\\ No newline at end of file
--- a/B.daml
+++ b/B.daml
@@ -1,2 +1,2 @@
 module B where
-b = 1
+b = 2
`
  const hunks = parseUnifiedDiff(patch)
  assert.deepEqual(hunks.map((h) => h.file), ['A.daml', 'B.daml'])
  assert.deepEqual(hunks[0].rows.map((r) => [r.oldText, r.newText]), [['a = 1', 'a = 2']])
  const rows = wholeFileRows([hunks[0]], 'a = 1')
  assert.deepEqual(sides(rows).newText, 'a = 2')
})

test('only the words that differ are marked inside a changed line', () => {
  const [old, now] = intralineSegments('allocationSignatories unconfirmedAllocations,', 'allocationSignatories unconfirmedAllocations')
  assert.deepEqual(old.filter((s) => s.changed).map((s) => s.text), [','])
  assert.deepEqual(now.filter((s) => s.changed), [])
})

// Highlighted runs of `amount : Decimal` as the highlighter colours them.
const RUNS = [
  { text: '    amount ', color: 'black' },
  { text: ':', color: 'blue' },
  { text: ' ', color: 'black' },
  { text: 'Decimal', color: 'teal' },
]
const pieces = (marked) => marked.map((s) => [s.changed, s.runs.map((r) => [r.text, r.color])])

test('a mark inside one run cuts it into pieces of the same colour', () => {
  const marked = markRuns(RUNS, [
    { text: '    amount : Dec', changed: false },
    { text: 'im', changed: true },
    { text: 'al', changed: false },
  ])
  assert.deepEqual(pieces(marked), [
    [false, [['    amount ', 'black'], [':', 'blue'], [' ', 'black'], ['Dec', 'teal']]],
    [true, [['im', 'teal']]],
    [false, [['al', 'teal']]],
  ])
})

test('a mark across several runs keeps each run its colour', () => {
  const marked = markRuns(RUNS, [
    { text: '    amo', changed: false },
    { text: 'unt : Deci', changed: true },
    { text: 'mal', changed: false },
  ])
  assert.deepEqual(pieces(marked), [
    [false, [['    amo', 'black']]],
    [true, [['unt ', 'black'], [':', 'blue'], [' ', 'black'], ['Deci', 'teal']]],
    [false, [['mal', 'teal']]],
  ])
})

test('the marks of a changed pair fall on the highlighted words that differ', () => {
  const [, now] = intralineSegments('    amount : Int', '    amount : Decimal')
  const marked = markRuns(RUNS, now)
  assert.deepEqual(marked.filter((s) => s.changed).flatMap((s) => s.runs.map((r) => [r.text, r.color])), [['Decimal', 'teal']])
  assert.equal(marked.flatMap((s) => s.runs.map((r) => r.text)).join(''), '    amount : Decimal')
})

test('a changed segment of only whitespace is not marked', () => {
  const marked = markRuns([{ text: 'a  b', color: 'black' }], [
    { text: 'a', changed: false },
    { text: '  ', changed: true },
    { text: 'b', changed: false },
  ])
  assert.deepEqual(marked.map((s) => s.changed), [false, false, false])
})
