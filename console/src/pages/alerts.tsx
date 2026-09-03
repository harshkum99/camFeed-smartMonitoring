import { useEffect, useState } from "react"
import { BellOff, Check, Loader2, X } from "lucide-react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Empty, Notice, PageHeader, Pill, SEVERITY_TONE } from "@/components/bits"
import { api, type AlertRow } from "@/lib/api"
import { cn } from "@/lib/utils"

export function AlertsPage() {
  const [rows, setRows] = useState<AlertRow[] | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = () =>
    api
      .alerts(50)
      .then(setRows)
      .catch((e) => setError((e as Error).message))

  useEffect(() => {
    load()
  }, [])

  async function feedback(id: string, actioned: boolean) {
    setBusy(id)
    try {
      const r = await api.alertFeedback(id, actioned)
      await load()
      // A muted rule is a real event the operator needs to know about, not a silent state change.
      if (r.rule_muted) {
        toast.warning("Rule muted", { description: r.message })
      } else {
        toast.success(actioned ? "Marked as a real incident" : "Marked not an incident", {
          description: r.message,
        })
      }
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  return (
    <>
      <PageHeader title="Alerts">
        Tap <strong className="text-foreground">Not an incident</strong> on anything that
        shouldn't have fired. It is the only per-site labelled data we ever get, and it is what
        makes a noisy rule mute itself before you mute the whole application.
      </PageHeader>

      {error && (
        <Notice tone="critical" title="Could not load alerts">
          {error}
        </Notice>
      )}

      {!rows && (
        <div className="space-y-3">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-20" />
          ))}
        </div>
      )}

      {rows?.length === 0 && (
        <Empty>
          <BellOff className="mx-auto mb-3 size-6 opacity-40" />
          No alerts yet. Rules you activate will appear here.
        </Empty>
      )}

      <div className="space-y-3">
        {rows?.map((a) => (
          <Card
            key={a.alert_id}
            className={cn(
              "border-l-2",
              a.severity === "critical" && "border-l-critical",
              a.severity === "warn" && "border-l-warn",
              a.severity === "info" && "border-l-ok",
            )}
          >
            <CardContent className="flex flex-wrap items-center justify-between gap-4 p-4">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{a.rule ?? "Rule"}</span>
                  <Pill tone={SEVERITY_TONE[a.severity]}>{a.severity}</Pill>
                  {a.verified === true && <Pill tone="ok">AI confirmed</Pill>}
                  {a.verified === false && <Pill tone="warn">AI disagreed</Pill>}
                </div>
                <div className="mt-1 text-sm text-muted-foreground">
                  {a.camera}
                  <span className="mx-1.5">·</span>
                  <span className="font-mono text-xs">{new Date(a.ts).toLocaleString()}</span>
                </div>
              </div>

              <div className="flex shrink-0 gap-2">
                {a.feedback ? (
                  <Pill tone={a.feedback === "true_positive" ? "ok" : "muted"}>
                    {a.feedback === "true_positive" ? "Confirmed" : "Marked not an incident"}
                  </Pill>
                ) : (
                  <>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy === a.alert_id}
                      onClick={() => feedback(a.alert_id, true)}
                    >
                      {busy === a.alert_id ? (
                        <Loader2 className="size-3.5 animate-spin" />
                      ) : (
                        <Check className="size-3.5" />
                      )}
                      Real incident
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy === a.alert_id}
                      onClick={() => feedback(a.alert_id, false)}
                    >
                      <X className="size-3.5" />
                      Not an incident
                    </Button>
                  </>
                )}
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </>
  )
}
