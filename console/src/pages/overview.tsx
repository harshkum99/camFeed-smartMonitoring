import { useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { BellRing, CameraOff, Eye, ScanEye, ShieldCheck } from "lucide-react"
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from "recharts"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  CoverageBar,
  Empty,
  GRADE_TONE,
  Notice,
  PageHeader,
  Pill,
  SEVERITY_TONE,
  Stat,
} from "@/components/bits"
import {
  GRADE_MEANING,
  api,
  type AlertRow,
  type ActivityPoint,
  type AuditRow,
  type Camera,
  type Coverage,
  type RuleRow,
} from "@/lib/api"

export function OverviewPage() {
  const [cameras, setCameras] = useState<Camera[] | null>(null)
  const [coverage, setCoverage] = useState<Coverage | null>(null)
  const [rules, setRules] = useState<RuleRow[]>([])
  const [alerts, setAlerts] = useState<AlertRow[]>([])
  const [audit, setAudit] = useState<AuditRow[]>([])
  const [activity, setActivity] = useState<ActivityPoint[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    Promise.all([
      api.cameras(),
      api.coverage(24),
      api.rules(),
      api.alerts(20),
      api.audit(40),
      api.activity(48),
    ])
      .then(([c, cov, r, a, au, act]) => {
        setCameras(c)
        setCoverage(cov)
        setRules(r)
        setAlerts(a)
        setAudit(au)
        setActivity(act)
      })
      .catch((e) => setError((e as Error).message))
  }, [])

  if (error) {
    return (
      <>
        <PageHeader title="Overview" />
        <Notice tone="critical" title="Could not load the site">
          {error}
        </Notice>
      </>
    )
  }

  if (!cameras || !coverage) {
    return (
      <>
        <PageHeader title="Overview" />
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-28" />
          ))}
        </div>
      </>
    )
  }

  const recognition = cameras.filter((c) => c.grade === "recognition").length
  const degraded = cameras.filter((c) => c.grade === "degraded" || c.grade === "unservable").length
  const badClocks = cameras.filter((c) => !c.clock_ok).length
  const slowKeyframe = cameras.filter((c) => (c.gop_ms ?? 0) > 2000)
  const unactioned = alerts.filter((a) => !a.feedback).length
  const refusals = audit.filter((a) => a.refused).length

  const byGrade = ["recognition", "anpr", "detection", "degraded", "unservable"]
    .map((g) => ({ grade: g, count: cameras.filter((c) => c.grade === g).length }))
    .filter((r) => r.count > 0)

  return (
    <>
      <PageHeader title="Overview">
        The state of this site right now — what the cameras can support, what they missed, and
        what is waiting for a human.
      </PageHeader>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat
          label="Camera coverage, 24h"
          value={`${Math.round(coverage.coverage_pct * 100)}%`}
          hint={
            coverage.gaps.length
              ? `${coverage.gaps.length} gap${coverage.gaps.length > 1 ? "s" : ""} recorded`
              : "no gaps recorded"
          }
          tone={
            coverage.coverage_pct >= 0.95
              ? "ok"
              : coverage.coverage_pct >= 0.8
                ? "warn"
                : "critical"
          }
          icon={Eye}
        />
        <Stat
          label="Cameras onboarded"
          value={cameras.length}
          hint={`${recognition} recognition-grade · ${degraded} degraded`}
          icon={ScanEye}
        />
        <Stat
          label="Active rules"
          value={rules.filter((r) => r.enabled).length}
          hint={`${rules.length - rules.filter((r) => r.enabled).length} disabled`}
          icon={ShieldCheck}
        />
        <Stat
          label="Alerts awaiting review"
          value={unactioned}
          hint={unactioned ? "tap through on the Alerts page" : "nothing outstanding"}
          tone={unactioned > 5 ? "warn" : undefined}
          icon={BellRing}
        />
      </div>

      {/* Problems worth acting on, surfaced rather than buried in a survey PDF. */}
      {(coverage.gaps.length > 0 || badClocks > 0 || slowKeyframe.length > 0) && (
        <div className="mt-5 space-y-2.5">
          {coverage.gaps.length > 0 && (
            <Notice tone="warn" title="Cameras were dark during the last 24 hours">
              {coverage.gaps
                .slice(0, 3)
                .map((g) => `${g.camera} for ${g.minutes} min`)
                .join("; ")}
              {coverage.gaps.length > 3 && `, and ${coverage.gaps.length - 3} more`}. Any question
              covering that period will say so rather than answer a confident zero.
            </Notice>
          )}
          {badClocks > 0 && (
            <Notice tone="critical" title={`${badClocks} camera clock(s) are out of sync`}>
              Drifted clocks corrupt every time-range answer, and on some recorders they also
              cause correct passwords to be rejected.
            </Notice>
          )}
          {slowKeyframe.length > 0 && (
            <Notice tone="warn" title={`${slowKeyframe.length} camera(s) have a slow keyframe interval`}>
              {slowKeyframe.map((c) => `${c.name} (${c.gop_ms} ms)`).join(", ")} — alerts on these
              lag by roughly that much regardless of how fast the detector is. Shortening the
              sub-stream's I-frame interval fixes it.
            </Notice>
          )}
        </div>
      )}

      <div className="mt-5 grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle className="text-sm font-medium">Site activity</CardTitle>
            <p className="text-xs text-muted-foreground">
              Events per hour. Shift changes and quiet hours are visible, so an abnormal night
              stands out — and a flat band usually means a camera stopped rather than that
              nothing happened.
            </p>
          </CardHeader>
          <CardContent>
            {activity.length === 0 ? (
              <p className="py-16 text-center text-sm text-muted-foreground">
                No events recorded yet.
              </p>
            ) : (
              <div className="h-56">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={activity} margin={{ left: -20, right: 8, top: 4 }}>
                    <defs>
                      <linearGradient id="activityFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="var(--primary)" stopOpacity={0.35} />
                        <stop offset="100%" stopColor="var(--primary)" stopOpacity={0.02} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid vertical={false} stroke="var(--border)" />
                    <XAxis
                      dataKey="hour"
                      tickFormatter={(v: string) =>
                        new Date(v).toLocaleTimeString([], { hour: "2-digit" })
                      }
                      stroke="var(--muted-foreground)"
                      fontSize={11}
                      tickLine={false}
                      axisLine={false}
                      minTickGap={28}
                    />
                    <YAxis
                      stroke="var(--muted-foreground)"
                      fontSize={11}
                      tickLine={false}
                      axisLine={false}
                      allowDecimals={false}
                    />
                    <RTooltip
                      labelFormatter={(v) => new Date(v as string).toLocaleString()}
                      contentStyle={{
                        background: "var(--popover)",
                        border: "1px solid var(--border)",
                        borderRadius: 8,
                        fontSize: 12,
                      }}
                    />
                    <Area
                      type="monotone"
                      dataKey="events"
                      stroke="var(--primary)"
                      strokeWidth={2}
                      fill="url(#activityFill)"
                    />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium">Per-camera coverage</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {cameras.slice(0, 8).map((c) => {
              const gap = coverage.gaps.find((g) => g.camera === c.name)
              const pct = gap ? Math.max(0, 1 - gap.minutes / (coverage.hours * 60)) : 1
              return (
                <div key={c.camera_id}>
                  <div className="flex items-baseline justify-between gap-2 text-xs">
                    <span className="truncate" title={c.name}>
                      {c.name}
                    </span>
                    <span className="shrink-0 font-mono text-muted-foreground">
                      {Math.round(pct * 100)}%
                    </span>
                  </div>
                  <CoverageBar pct={pct} className="mt-1" />
                </div>
              )
            })}
            {cameras.length === 0 && <Empty>No cameras onboarded yet.</Empty>}
          </CardContent>
        </Card>
      </div>

      {/* Capability breakdown as a list rather than a chart. Five categories with counts in
          single digits is data a list conveys better — a chart here would be decoration, and
          the *meaning* of each grade is the part the operator actually needs. */}
      <Card className="mt-5">
        <CardHeader>
          <CardTitle className="text-sm font-medium">
            What this estate can actually support
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-2.5">
          {byGrade.map((g) => (
            <div key={g.grade} className="flex items-center gap-3">
              <span className="w-8 shrink-0 text-right font-mono text-sm font-medium tabular-nums">
                {g.count}
              </span>
              <Pill tone={GRADE_TONE[g.grade as keyof typeof GRADE_TONE]} className="w-28 justify-center">
                {g.grade}
              </Pill>
              <div className="h-1.5 w-24 shrink-0 overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full rounded-full bg-primary"
                  style={{ width: `${(g.count / cameras.length) * 100}%` }}
                />
              </div>
              <span className="min-w-0 truncate text-xs text-muted-foreground">
                {GRADE_MEANING[g.grade as keyof typeof GRADE_MEANING]}
              </span>
            </div>
          ))}
        </CardContent>
      </Card>

      <div className="mt-5 grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader className="flex-row items-center justify-between">
            <CardTitle className="text-sm font-medium">Recent alerts</CardTitle>
            <Link to="/alerts" className="text-xs text-primary hover:underline">
              View all
            </Link>
          </CardHeader>
          <CardContent>
            {alerts.length === 0 ? (
              <p className="py-6 text-center text-sm text-muted-foreground">
                No alerts yet. Rules you activate will appear here.
              </p>
            ) : (
              <ul className="space-y-2.5">
                {alerts.slice(0, 6).map((a) => (
                  <li key={a.alert_id} className="flex items-center gap-2.5 text-sm">
                    <Pill tone={SEVERITY_TONE[a.severity]}>{a.severity}</Pill>
                    <span className="truncate">{a.rule ?? "Rule"}</span>
                    <span className="ml-auto shrink-0 font-mono text-xs text-muted-foreground">
                      {new Date(a.ts).toLocaleString()}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex-row items-center justify-between">
            <CardTitle className="text-sm font-medium">Questions asked</CardTitle>
            <Link to="/audit" className="text-xs text-primary hover:underline">
              Full audit
            </Link>
          </CardHeader>
          <CardContent>
            {audit.length === 0 ? (
              <p className="py-6 text-center text-sm text-muted-foreground">
                Nothing asked yet.
              </p>
            ) : (
              <>
                <p className="mb-3 text-xs text-muted-foreground">
                  {audit.length} in the log, {refusals} answered with an honest refusal.
                </p>
                <ul className="space-y-2">
                  {audit.slice(0, 6).map((r, i) => (
                    <li key={i} className="flex items-start gap-2 text-sm">
                      {r.refused ? (
                        <CameraOff className="mt-0.5 size-3.5 shrink-0 text-warn" />
                      ) : (
                        <Eye className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                      )}
                      <span className="truncate" title={r.question}>
                        {r.question}
                      </span>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </CardContent>
        </Card>
      </div>
    </>
  )
}
