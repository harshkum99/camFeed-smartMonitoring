/** Small shared pieces. Kept together so a status colour or a count layout cannot drift between
 *  pages — an operator learning that amber means "needs a look" on one screen must not find it
 *  meaning something else on the next. */

import { useState } from "react"
import { AlertTriangle, Check, Info, X } from "lucide-react"
import { Card, CardContent } from "@/components/ui/card"
import { cn } from "@/lib/utils"
import { API_BASE, type Evidence, type Grade, type Severity } from "@/lib/api"
import { formatSiteTime } from "@/lib/site"

/* ---------- status ---------- */

export function StatusDot({ tone }: { tone: "ok" | "warn" | "critical" | "muted" }) {
  return (
    <span
      className={cn(
        "size-1.5 shrink-0 rounded-full",
        tone === "ok" && "bg-ok",
        tone === "warn" && "bg-warn",
        tone === "critical" && "bg-critical",
        tone === "muted" && "bg-muted-foreground",
      )}
    />
  )
}

export function Pill({
  tone = "muted",
  children,
  className,
}: {
  tone?: "ok" | "warn" | "critical" | "muted" | "primary"
  children: React.ReactNode
  className?: string
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] font-medium",
        tone === "ok" && "bg-ok/15 text-ok",
        tone === "warn" && "bg-warn/15 text-warn",
        tone === "critical" && "bg-critical/15 text-critical",
        tone === "primary" && "bg-primary/15 text-primary",
        tone === "muted" && "bg-muted text-muted-foreground",
        className,
      )}
    >
      {children}
    </span>
  )
}

export const SEVERITY_TONE: Record<Severity, "ok" | "warn" | "critical"> = {
  info: "ok",
  warn: "warn",
  critical: "critical",
}

export const GRADE_TONE: Record<Grade, "ok" | "warn" | "critical" | "muted"> = {
  recognition: "ok",
  anpr: "ok",
  detection: "muted",
  degraded: "warn",
  unservable: "critical",
}

/* ---------- numbers ---------- */

export function Stat({
  label,
  value,
  hint,
  tone,
  icon: Icon,
}: {
  label: string
  value: string | number
  hint?: string
  tone?: "ok" | "warn" | "critical"
  icon?: React.ComponentType<{ className?: string }>
}) {
  return (
    <Card>
      <CardContent className="p-5">
        <div className="flex items-start justify-between gap-2">
          <span className="text-xs font-medium text-muted-foreground">{label}</span>
          {Icon && <Icon className="size-4 text-muted-foreground" />}
        </div>
        <div
          className={cn(
            "mt-2 font-mono text-2xl font-semibold tracking-tight tabular-nums",
            tone === "ok" && "text-ok",
            tone === "warn" && "text-warn",
            tone === "critical" && "text-critical",
          )}
        >
          {value}
        </div>
        {hint && <p className="mt-1 text-xs text-muted-foreground">{hint}</p>}
      </CardContent>
    </Card>
  )
}

export function CoverageBar({ pct, className }: { pct: number; className?: string }) {
  const v = Math.max(0, Math.min(100, Math.round(pct * 100)))
  return (
    <div className={cn("h-1.5 w-full overflow-hidden rounded-full bg-muted", className)}>
      <div
        className={cn(
          "h-full rounded-full transition-all",
          v >= 95 ? "bg-ok" : v >= 80 ? "bg-warn" : "bg-critical",
        )}
        style={{ width: `${v}%` }}
      />
    </div>
  )
}

/* ---------- evidence ----------
 * Never paraphrase a claim you cannot attach a frame to. The stored frame is the original
 * picture; the box is drawn here, over it, so the evidence itself is never altered. When no frame
 * is held the placeholder stays deliberately obvious — a plausible-looking fake frame in a demo is
 * worse than an honest gap. */

export function EvidenceStrip({ items, tz }: { items: Evidence[]; tz?: string }) {
  if (!items.length) return null
  return (
    <div className="flex gap-3 overflow-x-auto pb-1">
      {items.slice(0, 14).map((e, i) => (
        <EvidenceFrame key={`${e.track_id}-${i}`} e={e} tz={tz} />
      ))}
    </div>
  )
}

function EvidenceFrame({ e, tz }: { e: Evidence; tz?: string }) {
  const [failed, setFailed] = useState(false)
  const [ratio, setRatio] = useState<number | null>(null)
  const attrs = Object.entries(e.attrs || {}).filter(([, v]) => typeof v === "number")
  const src = e.keyframe_url ? `${API_BASE}${e.keyframe_url}` : null
  const [x1, y1, x2, y2] = e.bbox ?? [0, 0, 0, 0]
  const when = formatSiteTime(e.ts, tz, { date: true, seconds: true })

  return (
    <figure className="w-56 shrink-0 overflow-hidden rounded-lg border bg-card">
      {src && !failed ? (
        <a href={src} target="_blank" rel="noreferrer" className="grid aspect-video place-items-center bg-muted">
          {/* The wrapper takes the frame's own aspect ratio, so the box's percentages are
              percentages of the picture actually shown. Cropping the frame to fill a 16:9 tile
              would misplace the box on a 704x576 DVR frame and could crop the subject out. */}
          <span
            className="relative block max-h-full max-w-full"
            style={ratio ? { aspectRatio: String(ratio), height: ratio < 16 / 9 ? "100%" : undefined, width: ratio >= 16 / 9 ? "100%" : undefined } : undefined}
          >
            <img
              src={src}
              alt={`${e.camera} at ${when}`}
              loading="lazy"
              onLoad={(ev) => {
                const img = ev.currentTarget
                if (img.naturalHeight) setRatio(img.naturalWidth / img.naturalHeight)
              }}
              onError={() => setFailed(true)}
              className="block size-full object-contain"
            />
            {e.bbox && ratio && (
              <span
                className="pointer-events-none absolute rounded-sm border-2 border-primary shadow-[0_0_0_1px_rgba(0,0,0,0.4)]"
                style={{
                  left: `${x1 * 100}%`,
                  top: `${y1 * 100}%`,
                  width: `${(x2 - x1) * 100}%`,
                  height: `${(y2 - y1) * 100}%`,
                }}
              />
            )}
          </span>
        </a>
      ) : src && failed ? (
        // A frame we hold but could not show — failed integrity check, or out of this
        // deployment's scope — is not the same as no frame, and must not look like it.
        <div className="grid aspect-video place-items-center bg-warn/10 px-2 text-center text-[10px] font-mono text-warn">
          frame not available — could not be verified or served
        </div>
      ) : (
        <div className="grid aspect-video place-items-center bg-[repeating-linear-gradient(45deg,var(--muted),var(--muted)_6px,transparent_6px,transparent_12px)] text-[10px] font-mono text-muted-foreground">
          no frame held
        </div>
      )}
      <figcaption className="space-y-0.5 p-2 text-[11px] leading-tight">
        <div className="font-mono font-medium">{when}</div>
        <div className="truncate text-muted-foreground" title={e.camera}>
          {e.camera}
        </div>
        <div className="font-mono text-muted-foreground">
          conf {e.confidence.toFixed(2)}
          {attrs.map(([k, v]) => ` · ${k} ${v}`)}
        </div>
      </figcaption>
    </figure>
  )
}

/* ---------- feedback surfaces ---------- */

export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <Card>
      <CardContent className="py-12 text-center text-sm text-muted-foreground">
        {children}
      </CardContent>
    </Card>
  )
}

export function Notice({
  tone = "warn",
  title,
  children,
}: {
  tone?: "ok" | "warn" | "critical"
  title?: string
  children: React.ReactNode
}) {
  const Icon = tone === "ok" ? Check : tone === "critical" ? X : AlertTriangle
  return (
    <div
      className={cn(
        "flex gap-2.5 rounded-lg border-l-2 px-3 py-2.5 text-sm",
        tone === "ok" && "border-l-ok bg-ok/10",
        tone === "warn" && "border-l-warn bg-warn/10",
        tone === "critical" && "border-l-critical bg-critical/10",
      )}
    >
      <Icon
        className={cn(
          "mt-0.5 size-4 shrink-0",
          tone === "ok" && "text-ok",
          tone === "warn" && "text-warn",
          tone === "critical" && "text-critical",
        )}
      />
      <div className="min-w-0">
        {title && <div className="font-medium">{title}</div>}
        <div className="text-muted-foreground">{children}</div>
      </div>
    </div>
  )
}

export function PageHeader({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <header className="mb-6">
      <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
      {children && (
        <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{children}</p>
      )}
    </header>
  )
}

export function InfoHint({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
      <Info className="size-3" />
      {children}
    </span>
  )
}
