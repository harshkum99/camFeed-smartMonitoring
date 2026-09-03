import { useEffect, useState } from "react"
import { Loader2, Wand2 } from "lucide-react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Textarea } from "@/components/ui/textarea"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Empty, Notice, PageHeader, Pill, SEVERITY_TONE } from "@/components/bits"
import { api, type RuleDraft, type RuleRow } from "@/lib/api"

export function RulesPage() {
  const [text, setText] = useState("")
  const [draft, setDraft] = useState<RuleDraft | null>(null)
  const [rows, setRows] = useState<RuleRow[] | null>(null)
  const [drafting, setDrafting] = useState(false)
  const [saving, setSaving] = useState(false)

  const load = () => api.rules().then(setRows).catch((e) => toast.error((e as Error).message))
  useEffect(() => {
    load()
  }, [])

  async function preview() {
    if (!text.trim()) return
    setDrafting(true)
    setDraft(null)
    try {
      setDraft(await api.draftRule(text))
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setDrafting(false)
    }
  }

  async function save() {
    if (!draft?.rule) return
    setSaving(true)
    try {
      const r = await api.saveRule(draft.rule)
      toast.success("Rule activated", { description: r.name })
      setText("")
      setDraft(null)
      await load()
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <PageHeader title="Rules">
        Describe what you want watched. We compile it, then read it back to you in plain English —
        including how soon it can really fire and how often — before anything goes live.
      </PageHeader>

      <Card className="mb-6">
        <CardContent className="space-y-3 p-4">
          <Textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={2}
            placeholder="e.g. alert me if anyone enters the chemical store after 8pm"
          />
          <div className="flex flex-wrap gap-2">
            <Button onClick={preview} disabled={drafting || !text.trim()}>
              {drafting ? <Loader2 className="size-4 animate-spin" /> : <Wand2 className="size-4" />}
              Preview rule
            </Button>
            <Button variant="outline" onClick={save} disabled={!draft?.ok || saving}>
              {saving && <Loader2 className="size-4 animate-spin" />}
              Save &amp; activate
            </Button>
          </div>

          {draft && !draft.ok && (
            <Notice tone="warn" title="I couldn't turn that into a rule">
              {draft.reason} Try naming a place, a thing to watch for, and when.
            </Notice>
          )}

          {/* The read-back is the safety mechanism. An operator confirms this sentence before
              anything runs unattended for a month. */}
          {draft?.ok && draft.explain && (
            <div className="rounded-lg border bg-muted/40 p-4">
              <Pill tone="primary" className="mb-2">
                Read this back before saving
              </Pill>
              <pre className="whitespace-pre-wrap font-mono text-xs leading-relaxed text-muted-foreground">
                {draft.explain}
              </pre>
              {!!draft.unresolved?.length && (
                <p className="mt-2 text-xs text-warn">
                  Could not identify {draft.unresolved.join(", ")}.
                </p>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium">Active rules</CardTitle>
        </CardHeader>
        <CardContent>
          {rows?.length === 0 ? (
            <Empty>No rules yet. Describe one above.</Empty>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Rule</TableHead>
                    <TableHead>Watches for</TableHead>
                    <TableHead>Severity</TableHead>
                    <TableHead>Precision</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows?.map((r) => <RuleLine key={r.rule_id} r={r} onChange={load} />)}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </>
  )
}

function RuleLine({ r, onChange }: { r: RuleRow; onChange: () => void }) {
  const t = r.trigger as Record<string, unknown>
  const what = [t.type, t.object_class, t.zone && `in ${t.zone}`, t.dwell_seconds && `${t.dwell_seconds}s`]
    .filter(Boolean)
    .join(" · ")

  // Precision is in the list because a rule whose precision is falling is the one about to get
  // the whole product muted.
  const tone =
    r.precision === null ? "muted" : r.precision >= 0.8 ? "ok" : r.precision >= 0.5 ? "warn" : "critical"

  return (
    <TableRow>
      <TableCell className="font-medium">
        {r.name}
        {!r.enabled && (
          <Pill className="ml-2" tone="muted">
            disabled
          </Pill>
        )}
      </TableCell>
      <TableCell className="font-mono text-xs text-muted-foreground">{what}</TableCell>
      <TableCell>
        <Pill tone={SEVERITY_TONE[r.severity]}>{r.severity}</Pill>
      </TableCell>
      <TableCell>
        <Pill tone={tone}>{r.precision === null ? "—" : `${Math.round(r.precision * 100)}%`}</Pill>
        <span className="ml-2 font-mono text-xs text-muted-foreground">
          {r.true_positives}✓ {r.false_positives}✗
        </span>
      </TableCell>
      <TableCell className="text-right">
        {r.enabled && (
          <Button
            size="sm"
            variant="ghost"
            onClick={async () => {
              try {
                await api.disableRule(r.rule_id)
                toast.success("Rule disabled", {
                  description: "Kept, not deleted — the alerts it raised stay explainable.",
                })
                onChange()
              } catch (e) {
                toast.error((e as Error).message)
              }
            }}
          >
            Disable
          </Button>
        )}
      </TableCell>
    </TableRow>
  )
}
