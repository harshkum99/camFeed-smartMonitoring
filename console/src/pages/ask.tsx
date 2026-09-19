import { useState } from "react"
import { Link } from "react-router-dom"
import { CornerDownLeft, FileCheck2, Loader2, Sparkles } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import {
  CoverageBar,
  EvidenceStrip,
  Notice,
  PageHeader,
  Pill,
} from "@/components/bits"
import { REFUSAL_HELP, REFUSAL_LABEL, api, type Answer, type BundleSummary } from "@/lib/api"
import { formatSiteTime, useSite } from "@/lib/site"
import { cn } from "@/lib/utils"

const FACTORY_EXAMPLES = [
  "How many workers entered Zone B without a helmet yesterday?",
  "Was the fire exit blocked at any point yesterday?",
  "Show me anyone at the canteen door on Tuesday.",
  "How many people came through Gate 3 this morning?",
]

export function AskPage() {
  const site = useSite()
  const examples = site?.examples.length ? site.examples : FACTORY_EXAMPLES
  const [q, setQ] = useState("")
  const [answer, setAnswer] = useState<Answer | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function run(question: string) {
    if (!question.trim()) return
    setBusy(true)
    setError(null)
    try {
      setAnswer(await api.ask(question))
    } catch (e) {
      setAnswer(null)
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <PageHeader title="Ask your cameras">
        Ask in plain English. Every answer shows the evidence behind it, how much of the period
        the cameras actually covered, and how the question was understood — so a misread question
        is obvious rather than invisible.
      </PageHeader>

      <Card className="mb-5">
        <CardContent className="p-4">
          <div className="flex gap-2">
            <div className="relative flex-1">
              <Sparkles className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && run(q)}
                placeholder={`e.g. ${examples[0]}`}
                className="pl-9"
              />
            </div>
            <Button onClick={() => run(q)} disabled={busy || !q.trim()}>
              {busy ? <Loader2 className="size-4 animate-spin" /> : <CornerDownLeft className="size-4" />}
              Ask
            </Button>
          </div>

          <div className="mt-3 flex flex-wrap gap-1.5">
            {examples.map((ex) => (
              <button
                key={ex}
                onClick={() => {
                  setQ(ex)
                  run(ex)
                }}
                className="rounded-md border px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:border-primary hover:text-foreground"
              >
                {ex}
              </button>
            ))}
          </div>
        </CardContent>
      </Card>

      {error && (
        <Notice tone="critical" title="Something went wrong">
          {error}
        </Notice>
      )}

      {answer && <AnswerCard a={answer} />}
    </>
  )
}

function AnswerCard({ a }: { a: Answer }) {
  const refused = a.refused || !!a.abstain_reason
  const cov = Math.round(a.coverage_pct * 100)
  // Recorders start files a few seconds apart, so a window rarely aligns with a clip to the second.
  // Those sub-minute holes still count against coverage; they are just not worth an alert box.
  const gaps = a.gaps.filter((g) => !g.recorded || g.seconds >= 60)

  return (
    <Card
      className={cn(
        "border-l-2",
        refused ? "border-l-warn" : "border-l-primary",
      )}
    >
      <CardContent className="space-y-4 p-5">
        <div>
          {a.abstain_reason && (
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <Pill tone="warn">{REFUSAL_LABEL[a.abstain_reason]}</Pill>
              <span className="text-xs text-muted-foreground">
                {REFUSAL_HELP[a.abstain_reason]}
              </span>
            </div>
          )}
          {a.refused && !a.abstain_reason && (
            <Pill tone="warn" className="mb-2">
              Could not interpret
            </Pill>
          )}
          <p className="text-base font-medium leading-snug">{a.message}</p>
        </div>

        {/* The triple, never a bare scalar. A single number hides that the database counted
            whatever rows existed at whatever confidence, over whatever the cameras saw. */}
        {!refused && a.total > 0 && (
          <div className="flex flex-wrap gap-8">
            <Figure value={a.confident} label="confident" />
            {a.ambiguous > 0 && (
              <Figure value={a.ambiguous} label="need a look" tone="warn" />
            )}
            <div className="min-w-32">
              <div className="font-mono text-2xl font-semibold tabular-nums">{cov}%</div>
              <div className="text-xs text-muted-foreground">camera coverage</div>
              <CoverageBar pct={a.coverage_pct} className="mt-1.5" />
            </div>
          </div>
        )}

        {a.limits.length > 0 && (
          <Notice tone="warn" title="What these cameras can and cannot see">
            {a.limits.map((l) => l.message).join("; ")}.
          </Notice>
        )}

        {a.notes.length > 0 && (
          <Notice tone="warn" title="Not measured everywhere">
            {a.notes.join("; ")}.
          </Notice>
        )}

        {gaps.length > 0 && (
          <Notice tone="warn" title="Coverage gaps in this period">
            {gaps
              .slice(0, 3)
              .map((g) =>
                g.recorded
                  ? `${g.camera}: no footage held for ${fmtDuration(g.seconds)}`
                  : `${g.camera} down ${g.minutes} min`,
              )
              .join("; ")}
            {gaps.length > 3 && `, and ${gaps.length - 3} more`}.
          </Notice>
        )}

        {a.unresolved.length > 0 && (
          <Notice tone="warn" title="Not everything in your question was recognised">
            Could not identify {a.unresolved.join(", ")} at this site.
          </Notice>
        )}

        {a.evidence.length > 0 && (
          <div className="space-y-2">
            <Pill tone="primary">{a.evidence.length} evidence frames</Pill>
            <EvidenceStrip items={a.evidence} tz={a.site_tz} />
          </div>
        )}

        {a.evidence.some((e) => e.keyframe_url) && <PrepareBundle a={a} />}

        {/* The cheapest correctness control in the whole query path: a person reading this spots
            a misread question instantly. */}
        {a.describes && (
          <div className="border-t pt-3 font-mono text-[11px] leading-relaxed text-muted-foreground">
            <span className="text-foreground">Understood as:</span> {a.describes}
            <span className="mx-1.5">·</span>
            {a.as_of && (
              <>
                answered as of {formatSiteTime(a.as_of, a.site_tz, { date: true })}
                <span className="mx-1.5">·</span>
              </>
            )}
            {a.latency_ms} ms
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function Figure({
  value,
  label,
  tone,
}: {
  value: number
  label: string
  tone?: "warn"
}) {
  return (
    <div>
      <div
        className={cn(
          "font-mono text-2xl font-semibold tabular-nums",
          tone === "warn" && "text-warn",
        )}
      >
        {value}
      </div>
      <div className="text-xs text-muted-foreground">{label}</div>
    </div>
  )
}

function fmtDuration(seconds: number) {
  const m = Math.floor(seconds / 60)
  if (m < 1) return "under a minute"
  if (m < 60) return `${m} min`
  const h = Math.floor(m / 60)
  return m % 60 ? `${h} h ${m % 60} min` : `${h} h`
}

/** Turn the evidence behind an answer into a bundle a person in charge of the recorder and an expert can certify.
 *  A purpose is required: it is printed on the draft certificate, and a bundle nobody can
 *  explain the reason for is the first thing opposing counsel asks about. */
function PrepareBundle({ a }: { a: Answer }) {
  const [open, setOpen] = useState(false)
  const [purpose, setPurpose] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<BundleSummary | null>(null)

  async function prepare() {
    setBusy(true)
    setError(null)
    try {
      const ids = a.evidence.filter((e) => e.keyframe_url).map((e) => e.track_id)
      setDone(await api.createBundle(ids, purpose.trim(), a.question))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  if (done) {
    return (
      <Notice tone="ok" title="Evidence bundle prepared">
        {done.sources.length} original recording(s) and {done.frames} frame(s), sealed under
        Merkle root <span className="font-mono text-xs">{done.merkle_root.slice(0, 16)}…</span>.{" "}
        <a className="underline" href={api.bundleCertificateUrl(done.bundle_id)} target="_blank" rel="noreferrer">
          Open the certificate draft
        </a>{" "}
        · <Link className="underline" to="/evidence">all bundles</Link>
      </Notice>
    )
  }

  if (!open) {
    return (
      <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
        <FileCheck2 className="size-4" />
        Prepare evidence bundle
      </Button>
    )
  }

  return (
    <div className="space-y-2 rounded-lg border p-3">
      <div className="text-sm font-medium">Prepare an evidence bundle from these frames</div>
      <p className="text-xs text-muted-foreground">
        Copies the original recordings, checks each against the hash taken when it was imported,
        and drafts a section 63 certificate for the person in charge of the recorder and an expert to complete and sign.
        It does not certify anything itself.
      </p>
      <Textarea
        value={purpose}
        onChange={(e) => setPurpose(e.target.value)}
        placeholder="Purpose, e.g. Complaint 42 — stairwell incident, for the police"
        rows={2}
      />
      {error && <Notice tone="critical" title="Bundle not prepared">{error}</Notice>}
      <div className="flex gap-2">
        <Button size="sm" onClick={prepare} disabled={busy || purpose.trim().length < 3}>
          {busy ? <Loader2 className="size-4 animate-spin" /> : <FileCheck2 className="size-4" />}
          Prepare
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
          Cancel
        </Button>
      </div>
    </div>
  )
}
