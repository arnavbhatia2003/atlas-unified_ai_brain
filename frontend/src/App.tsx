import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom'

import { AppShell } from '@/components/AppShell'
import { Landing } from '@/pages/Landing'
import { Home } from '@/pages/Home'
import { KnowledgeGraph } from '@/pages/KnowledgeGraph'
import { AskCopilot } from '@/pages/AskCopilot'
import { Workflows } from '@/pages/Workflows'
import { Connectors } from '@/pages/Connectors'
import { Rca } from '@/pages/workflows/Rca'
import { Compliance } from '@/pages/workflows/Compliance'
import { Documents } from '@/pages/workflows/Documents'
import { Assets } from '@/pages/Assets'
import { Review } from '@/pages/Review'

/**
 * `/` is the public landing page (own nav + footer, no app shell). Every other
 * route renders inside the operational app shell; the dashboard lives at /home.
 */
function AppRoutes() {
  const location = useLocation()
  if (location.pathname === '/') return <Landing />
  return (
    <AppShell>
      <Routes>
        <Route path="/home" element={<Home />} />
        <Route path="/graph" element={<KnowledgeGraph />} />
        <Route path="/ask" element={<AskCopilot />} />
        <Route path="/workflows" element={<Workflows />} />
        <Route path="/connectors" element={<Connectors />} />
        <Route path="/workflows/ingest" element={<Navigate to="/connectors" replace />} />
        <Route path="/workflows/documents" element={<Documents />} />
        <Route path="/workflows/rca" element={<Rca />} />
        <Route path="/workflows/compliance" element={<Compliance />} />
        <Route path="/assets" element={<Assets />} />
        <Route path="/review" element={<Review />} />
        <Route path="*" element={<Navigate to="/home" replace />} />
      </Routes>
    </AppShell>
  )
}

function App() {
  return (
    <BrowserRouter>
      <AppRoutes />
    </BrowserRouter>
  )
}

export default App
