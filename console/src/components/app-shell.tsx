import { NavLink } from "react-router-dom"
import {
  Activity,
  BellRing,
  Camera,
  LayoutDashboard,
  ScrollText,
  Search,
  Shapes,
  ShieldCheck,
} from "lucide-react"
import { cn } from "@/lib/utils"
import type { Coverage } from "@/lib/api"
import { formatSiteTime, useSite } from "@/lib/site"

const NAV = [
  { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
  { to: "/ask", label: "Ask", icon: Search },
  { to: "/alerts", label: "Alerts", icon: BellRing },
  { to: "/rules", label: "Rules", icon: ShieldCheck },
  { to: "/zones", label: "Zones", icon: Shapes },
  { to: "/cameras", label: "Cameras", icon: Camera },
  { to: "/audit", label: "Audit", icon: ScrollText },
]

interface Props {
  children: React.ReactNode
  coverage: Coverage | null
  interpreter: string | null
  online: boolean
}

export function AppShell({ children, coverage, interpreter, online }: Props) {
  const site = useSite()
  const pct = coverage ? Math.round(coverage.coverage_pct * 100) : null
  // Coverage lives in the chrome, not on a page. An operator who cannot see that a camera has
  // been dark since Tuesday reads every empty answer about it as good news.
  const tone = pct === null ? "muted" : pct >= 95 ? "ok" : pct >= 80 ? "warn" : "critical"

  return (
    <div className="flex min-h-screen">
      <aside className="hidden w-60 shrink-0 flex-col border-r border-sidebar-border bg-sidebar md:flex">
        <div className="flex h-14 items-center gap-2 border-b border-sidebar-border px-5">
          <div className="grid size-7 place-items-center rounded-md bg-primary">
            <Activity className="size-4 text-primary-foreground" />
          </div>
          <div className="leading-tight">
            <div className="text-sm font-semibold">Smart Cam</div>
            <div className="text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
              Monitoring
            </div>
          </div>
        </div>

        <nav className="flex-1 space-y-0.5 p-3">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors",
                  isActive
                    ? "bg-accent font-medium text-accent-foreground"
                    : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
                )
              }
            >
              <Icon className="size-4" />
              {label}
            </NavLink>
          ))}
        </nav>

        <div className="border-t border-sidebar-border p-4 text-xs text-muted-foreground">
          <div className="flex items-center gap-2">
            <span
              className={cn(
                "size-1.5 rounded-full",
                online ? "bg-ok" : "bg-critical",
              )}
            />
            {online ? "API connected" : "API unreachable"}
          </div>
          <div className="mt-1 font-mono text-[11px]">
            {interpreter === "GeminiProvider" ? "Gemini interpreter" : "offline interpreter"}
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex h-14 items-center gap-4 border-b bg-background/80 px-5 backdrop-blur">
          <nav className="flex gap-1 overflow-x-auto md:hidden">
            {NAV.map(({ to, label, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                className={({ isActive }) =>
                  cn(
                    "whitespace-nowrap rounded-md px-2.5 py-1.5 text-xs",
                    isActive ? "bg-accent font-medium" : "text-muted-foreground",
                  )
                }
              >
                {label}
              </NavLink>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-4 text-xs text-muted-foreground">
            {coverage && (
              <>
                <span className="hidden font-mono sm:inline">
                  {coverage.cameras} cameras
                </span>
                <span className="flex items-center gap-2">
                  <span
                    className={cn(
                      "size-1.5 rounded-full",
                      tone === "ok" && "bg-ok",
                      tone === "warn" && "bg-warn",
                      tone === "critical" && "bg-critical",
                      tone === "muted" && "bg-muted-foreground",
                    )}
                  />
                  <span className="font-mono">{pct}% covered</span>
                  <span className="hidden lg:inline">
                    {site?.replay ? "last 24h of the recording" : "last 24h"}
                  </span>
                </span>
              </>
            )}
          </div>
        </header>

        {site?.replay && site.as_of && (
          // A recording answered as if it were live would put every relative word — "today",
          // "this morning" — on the wrong day. Say so on every page, not in a help text.
          <div className="border-b bg-primary/10 px-5 py-2 text-xs lg:px-7">
            <span className="font-medium">Recorded footage</span>
            <span className="text-muted-foreground">
              {" "}· {site.name} · questions are answered as of{" "}
              {formatSiteTime(site.as_of, site.tz, { date: true })}
            </span>
          </div>
        )}

        <main className="min-w-0 flex-1 p-5 lg:p-7">{children}</main>

        {site?.attribution && (
          <footer className="border-t px-5 py-3 text-[11px] text-muted-foreground lg:px-7">
            Footage: {site.attribution}
          </footer>
        )}
      </div>
    </div>
  )
}
