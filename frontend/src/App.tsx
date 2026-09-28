import { Layout } from 'antd'
import { AgentPage } from './AgentPage'

const { Header, Content } = Layout

export function App() {
  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{ display: 'flex', alignItems: 'center', padding: '0 24px' }}>
        <div style={{ color: 'white', fontWeight: 'bold', fontSize: '18px' }}>Daml Agent Benchmark</div>
      </Header>
      <Content style={{ padding: '24px' }}>
        <AgentPage />
      </Content>
    </Layout>
  )
}
