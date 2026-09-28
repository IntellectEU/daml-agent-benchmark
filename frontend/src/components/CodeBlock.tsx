// A scrolling, syntax-highlighted block of text. The task detail shows code, diffs and
// JSON in several places; they share one font, padding and corner radius and differ only
// in language, theme, height and whether the lines are numbered. A block with a
// `lineClassName` is the side-by-side diff: it numbers its lines, wraps them, gives each
// line the class the callback returns and is set slightly looser.

import SyntaxHighlighter from 'react-syntax-highlighter'
import { atomOneDark, github } from 'react-syntax-highlighter/dist/esm/styles/hljs'

const THEMES = {
  dark: atomOneDark,
  light: github,
}

type CodeBlockProps = {
  language: string
  text: string
  maxHeight: string
  theme?: keyof typeof THEMES
  margin?: string
  lineClassName?: (lineNumber: number) => string
}

export function CodeBlock({ language, text, maxHeight, theme = 'dark', margin = '0', lineClassName }: CodeBlockProps) {
  const numbered = lineClassName !== undefined
  return (
    <SyntaxHighlighter
      language={language}
      style={THEMES[theme]}
      showLineNumbers={numbered}
      wrapLines={numbered}
      wrapLongLines={!numbered}
      lineProps={numbered ? (lineNumber) => ({ className: lineClassName(lineNumber) }) : undefined}
      customStyle={{
        margin,
        maxHeight,
        overflow: 'auto',
        borderRadius: '8px',
        padding: '8px 10px',
        fontFamily: '"IBM Plex Mono", "SFMono-Regular", Menlo, monospace',
        fontSize: '12px',
        lineHeight: numbered ? 1.45 : 1.4,
      }}
      codeTagProps={{ style: { fontFamily: 'inherit' } }}
    >
      {text}
    </SyntaxHighlighter>
  )
}
