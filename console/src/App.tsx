import { useEffect, useState } from "react"
import { BrowserRouter, Route, Routes } from "react-router-dom"
import { Toaster } from "@/components/ui/sonner"
import { AppShell } from "@/components/app-shell"
import { OverviewPage } from "@/pages/overview"
import { AskPage } from "@/pages/ask"
import { AlertsPage } from "@/pages/alerts"
import { RulesPage } from "@/pages/rules"
import { ZonesPage } from "@/pages/zones"
import { CamerasPage } from "@/pages/cameras"
import { AuditPage } from "@/pages/audit"
import { EvidencePage } from "@/pages/evidence"
import { api, type Coverage } from "@/lib/api"
import { SiteProvider } from "@/lib/site"

export default function App() {
  const [coverage, setCoverage] = useState<Coverage | null>(null)
  const [interpreter, setInterpreter] = useState<string | null>(null)
  const [online, setOnline] = useState(true)

  useEffect(() => {
    Promise.all([api.health(), api.coverage(24)])
      .then(([h, c]) => {
        setInterpreter(h.interpreter)
        setCoverage(c)
        setOnline(true)
      })
      .catch(() => setOnline(false))
  }, [])

  return (
    <SiteProvider>
    <BrowserRouter>
      <AppShell coverage={coverage} interpreter={interpreter} online={online}>
        <Routes>
          <Route path="/" element={<OverviewPage />} />
          <Route path="/ask" element={<AskPage />} />
          <Route path="/alerts" element={<AlertsPage />} />
          <Route path="/rules" element={<RulesPage />} />
          <Route path="/zones" element={<ZonesPage />} />
          <Route path="/cameras" element={<CamerasPage />} />
          <Route path="/evidence" element={<EvidencePage />} />
          <Route path="/audit" element={<AuditPage />} />
        </Routes>
      </AppShell>
      <Toaster position="bottom-right" />
    </BrowserRouter>
    </SiteProvider>
  )
}
