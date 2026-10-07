// The mutation catalogue's Daml highlighter: Shiki with the TextMate grammar of the Daml
// VS Code extension, our grammar of extra scopes injected into it, and VS Code's default
// light theme, on Shiki's JavaScript regex engine. Shiki and the grammars load on first use,
// in chunks of their own, so the runs page does not carry them. Until they arrive, lines come
// back as plain text.

import { useEffect, useState } from 'react'
import type { HighlighterCore, LanguageRegistration, ThemeRegistrationRaw } from 'shiki/core'

const THEME = 'daml-light'

// Colours of the light theme darkened to reach 4.5:1 contrast on the code background.
const DARKER: Record<string, string> = {
  '#267f99': '#16697f', // types, constructors and module names
  '#098658': '#07704a', // numbers
}

// A run of highlighted code. `fontStyle` holds Shiki's bits: 1 italic, 2 bold, 4 underline.
export type CodeRun = { text: string; color?: string; fontStyle?: number }

let loading: Promise<HighlighterCore> | null = null
let loaded: HighlighterCore | null = null

export function loadHighlighter(): Promise<HighlighterCore> {
  loading ??= Promise.all([
    import('shiki/core'),
    import('shiki/engine/javascript'),
    import('shiki/themes/light-plus.mjs'),
    import('./grammars/daml.tmLanguage.json', { with: { type: 'json' } }),
    import('./grammars/daml-extras.tmLanguage.json', { with: { type: 'json' } }),
  ]).then(async ([{ createHighlighterCore }, { createJavaScriptRegexEngine }, lightPlus, grammar, extras]) => {
    const theme: ThemeRegistrationRaw = {
      ...lightPlus.default,
      name: THEME,
      tokenColors: [
        ...(lightPlus.default.tokenColors ?? []).map((rule) => {
          const darker = DARKER[rule.settings.foreground?.toLowerCase() ?? '']
          return darker === undefined ? rule : { ...rule, settings: { ...rule.settings, foreground: darker } }
        }),
        // A type in a signature takes the colour of a capitalised name elsewhere, not the keywords' blue.
        { scope: 'storage.type.daml', settings: { foreground: DARKER['#267f99'] } },
      ],
    }
    loaded = await createHighlighterCore({
      themes: [theme],
      // The upstream JSON's inferred type is looser than the grammar schema it follows.
      langs: [grammar.default as LanguageRegistration, extras.default],
      engine: createJavaScriptRegexEngine(),
    })
    return loaded
  })
  return loading
}

// The highlighter once it has loaded, and null before. The first call starts the load.
export function useDamlHighlighter(): HighlighterCore | null {
  const [highlighter, setHighlighter] = useState(loaded)
  useEffect(() => {
    if (highlighter === null) loadHighlighter().then(setHighlighter)
  }, [highlighter])
  return highlighter
}

// Consecutive lines of one Daml file, each as its runs. They are highlighted together, so a
// line inside a block comment or a multi-line string reads as one.
export function highlightLines(highlighter: HighlighterCore | null, lines: string[]): CodeRun[][] {
  if (highlighter === null) return lines.map((line) => [{ text: line }])
  return highlighter
    .codeToTokensBase(lines.join('\n'), { lang: 'daml', theme: THEME })
    .map((tokens) => tokens.map((t) => ({ text: t.content, color: t.color, fontStyle: t.fontStyle })))
}
