import { useEffect, useState } from 'react'
import { Layout } from 'antd'
import { AgentPage } from './AgentPage'
import { MutationCatalogue } from './components/MutationCatalogue'

const { Header, Content } = Layout

// Which page the address names: the runs by default, or the mutation catalogue at
// `#mutations`, with a task chosen at `#mutations/<task file name>`.
type View = { page: 'runs' } | { page: 'mutations'; task: string | null }

const MUTATIONS_HASH = 'mutations'

function viewFromHash(): View {
  const [page, task] = window.location.hash.slice(1).split('/', 2)
  return page === MUTATIONS_HASH ? { page: 'mutations', task: task ? decodeURIComponent(task) : null } : { page: 'runs' }
}

export function App() {
  const [view, setView] = useState<View>(viewFromHash)
  useEffect(() => {
    const onHashChange = () => setView(viewFromHash())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{ display: 'flex', alignItems: 'center', padding: '0 24px' }}>
        <div style={{ color: 'white', fontWeight: 'bold', fontSize: '18px' }}>Daml Agent Benchmark</div>
      </Header>
      <Content style={{ padding: '24px' }}>
        {/* The runs page stays mounted under the catalogue, so coming back keeps its sorting, page and selection. */}
        <div style={{ display: view.page === 'runs' ? undefined : 'none' }}>
          <AgentPage />
        </div>
        {view.page === 'mutations' && (
          <MutationCatalogue
            selectedFileName={view.task}
            onSelect={(task) => {
              // Picking a task replaces the address rather than adding to the history, so Back returns to the runs.
              window.history.replaceState(null, '', `#${MUTATIONS_HASH}/${encodeURIComponent(task)}`)
              setView({ page: 'mutations', task })
            }}
            onBack={() => {
              window.location.hash = ''
            }}
          />
        )}
      </Content>
    </Layout>
  )
}
