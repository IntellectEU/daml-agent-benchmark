// Tests of the catalogue's Daml highlighter: the real Shiki highlighter with the Daml
// grammar and the injected extra scopes, on a small snippet.
//
//     npm test

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { highlightLines, loadHighlighter } from '../src/highlighter.ts'

const SNIPPET = [
  '    do',
  '      unlocked <- mapA (\\i -> exercise i Unlock) ious',
  '      create this with locker = owner',
  '      assertMsg "create Unlock" $ owner /= locker -- exercise Unlock',
].join('\n')

// Each word of the snippet with the scopes it gets, by line.
async function scopesByLine() {
  const highlighter = await loadHighlighter()
  return highlighter
    .codeToTokensBase(SNIPPET, { lang: 'daml', theme: 'daml-light', includeExplanation: true })
    .map((tokens) =>
      tokens
        .flatMap((t) => t.explanation)
        .filter((e) => e.content.trim())
        .map((e) => [e.content.trim(), e.scopes.map((s) => s.scopeName)])
    )
}

const has = (scopes, prefix) => scopes.some((s) => s.startsWith(prefix))

test('ledger actions and constructors in code get the extra scopes', async () => {
  const lines = await scopesByLine()
  const scopes = (line, word) => lines[line].find(([text]) => text === word)[1]
  assert.ok(has(scopes(1, 'exercise'), 'support.function.daml-extras'))
  assert.ok(has(scopes(1, 'mapA'), 'support.function.daml-extras'))
  assert.ok(has(scopes(1, 'Unlock'), 'entity.name.type.constructor.daml-extras'))
  assert.ok(has(scopes(2, 'create'), 'support.function.daml-extras'))
  assert.ok(has(scopes(2, 'this'), 'variable.language.daml-extras'))
  assert.ok(has(scopes(3, 'assertMsg'), 'support.function.daml-extras'))
})

test('the same words inside a string or a comment keep only the string or comment scope', async () => {
  const lines = await scopesByLine()
  const inside = lines[3].filter(([text]) => /create|Unlock|exercise/.test(text) && text !== 'assertMsg')
  assert.equal(inside.length, 2)
  for (const [, scopes] of inside) {
    assert.ok(has(scopes, 'string') || has(scopes, 'comment'))
    assert.ok(!scopes.some((s) => s.endsWith('.daml-extras')))
  }
})

test('functions, constructors and keywords are drawn in different colours', async () => {
  const highlighter = await loadHighlighter()
  const [line] = highlightLines(highlighter, ['    do exercise i Unlock'])
  const colour = (word) => line.find((run) => run.text.trim() === word).color
  assert.equal(new Set([colour('do'), colour('exercise'), colour('Unlock')]).size, 3)
})
