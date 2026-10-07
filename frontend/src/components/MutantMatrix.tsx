import { Tooltip, Typography } from 'antd'
import type { AgentMutantMatrix } from '../types'
import { Sym } from './Sym'

const { Text } = Typography

// The script name without the path grading keys it by: `daml/Test.daml:testCancel` -> `testCancel`.
function scriptName(key: string): string {
  return key.slice(key.lastIndexOf(':') + 1)
}

/**
 * The agent's test scripts against the correct implementation and each mutant. A mutant is
 * caught when a script that passed on the correct code fails on it, or when the test file no
 * longer builds against it.
 */
export function MutantMatrix({ matrix }: { matrix: AgentMutantMatrix }) {
  const caught = matrix.rows.filter((row) => row.caught).length
  return (
    <div className="agent-mutant-matrix">
      <div className="agent-mutant-matrix-summary">
        {matrix.skipped
          ? <Text type="secondary">No mutant was built: {matrix.skipped}. The task catches none of its {matrix.total} mutants.</Text>
          : <Text><strong>{caught}</strong> of {matrix.total} mutants caught{matrix.rows.length < matrix.total ? ` (${matrix.rows.length} built: a capped run)` : ''}.</Text>}
      </div>
      <div className="agent-mutant-matrix-scroll">
        <table>
          <thead>
            <tr>
              <th>Mutant</th>
              <th>Caught</th>
              {matrix.scripts.map((script) => (
                <th key={script} title={script}><Text code>{scriptName(script)}</Text></th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr className="agent-mutant-matrix-correct">
              <td>correct implementation</td>
              <td />
              {matrix.scripts.map((script) => (
                <td key={script}>
                  <Sym s={matrix.correct[script] ? '✅' : '❌'} meaning={matrix.correct[script] ? 'Passes on the correct code' : 'Fails on the correct code'} />
                </td>
              ))}
            </tr>
            {matrix.rows.map((row) => (
              <tr key={row.id}>
                <td>
                  <Text code>{row.id}</Text>{' '}
                  <Text type="secondary">{row.kind}{row.source === 'real-bug' ? ' · real bug' : ''}</Text>
                </td>
                <td>
                  <Sym s={row.caught ? '🎯' : '🫥'} meaning={row.caught ? 'Caught by the tests' : 'Missed by the tests'} />
                </td>
                {row.results === null ? (
                  <td colSpan={matrix.scripts.length}>
                    <Tooltip title={row.error ?? ''} mouseEnterDelay={0.15}>
                      <Text type="secondary">the test file no longer builds against this mutant</Text>
                    </Tooltip>
                  </td>
                ) : (
                  matrix.scripts.map((script) => {
                    const passed = row.results?.[script]
                    return (
                      <td key={script}>
                        <Sym s={passed ? '✅' : '❌'} meaning={passed ? 'Passes on the mutant' : 'Fails on the mutant'} />
                      </td>
                    )
                  })
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
