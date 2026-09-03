import { useEffect, useState } from "react"
import { Card, CardContent } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Empty, Notice, PageHeader, Pill } from "@/components/bits"
import { REFUSAL_LABEL, api, type AuditRow } from "@/lib/api"

export function AuditPage() {
  const [rows, setRows] = useState<AuditRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.audit(80).then(setRows).catch((e) => setError((e as Error).message))
  }, [])

  const refused = rows?.filter((r) => r.refused).length ?? 0

  return (
    <>
      <PageHeader title="Audit">
        Every question asked of this site, including the ones that were refused and the ones we
        could not interpret. A plain-language search box over every camera in a building needs
        this, and so does any security review you will be asked to pass.
      </PageHeader>

      {error && (
        <Notice tone="critical" title="Could not load the audit log">
          {error}
        </Notice>
      )}

      {rows && rows.length > 0 && (
        <p className="mb-4 text-sm text-muted-foreground">
          {rows.length} questions logged, {refused} answered with an honest refusal. The log is
          append-only — it cannot be edited or deleted, including by us.
        </p>
      )}

      {!rows && <Skeleton className="h-64" />}
      {rows?.length === 0 && <Empty>No questions asked yet.</Empty>}

      {!!rows?.length && (
        <Card>
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>When</TableHead>
                    <TableHead>Who</TableHead>
                    <TableHead>Question</TableHead>
                    <TableHead>Outcome</TableHead>
                    <TableHead className="text-right">Coverage</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((r, i) => (
                    <TableRow key={i}>
                      <TableCell className="whitespace-nowrap font-mono text-xs text-muted-foreground">
                        {new Date(r.at).toLocaleString()}
                      </TableCell>
                      <TableCell className="text-sm">{r.actor}</TableCell>
                      <TableCell className="max-w-md text-sm">{r.question}</TableCell>
                      <TableCell>
                        {r.refused ? (
                          <Pill tone="warn">
                            {r.reason ? (REFUSAL_LABEL[r.reason] ?? "refused") : "refused"}
                          </Pill>
                        ) : (
                          <Pill tone="ok">{r.rows ?? 0} row(s)</Pill>
                        )}
                      </TableCell>
                      <TableCell className="text-right font-mono text-xs text-muted-foreground">
                        {r.coverage_pct === null ? "—" : `${Math.round(r.coverage_pct * 100)}%`}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </CardContent>
        </Card>
      )}
    </>
  )
}
