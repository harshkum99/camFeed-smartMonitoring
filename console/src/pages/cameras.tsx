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
import { Empty, GRADE_TONE, Notice, PageHeader, Pill } from "@/components/bits"
import { GRADE_MEANING, api, type Camera } from "@/lib/api"

export function CamerasPage() {
  const [rows, setRows] = useState<Camera[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.cameras().then(setRows).catch((e) => setError((e as Error).message))
  }, [])

  const noRecognition = rows?.length ? !rows.some((c) => c.grade === "recognition") : false

  return (
    <>
      <PageHeader title="Cameras">
        What each camera can actually support. A camera graded for detection cannot identify a
        face no matter what software runs on it — there are not enough pixels, and nothing
        recovers detail the sensor never captured.
      </PageHeader>

      {error && (
        <Notice tone="critical" title="Could not load cameras">
          {error}
        </Notice>
      )}

      {noRecognition && (
        <div className="mb-4">
          <Notice tone="warn" title="No camera on this site is recognition-grade">
            Face matching needs at least 64 pixels between the eye centres. Enabling it here would
            require repositioning or replacing a camera at each entry point. Everything else on
            this list works today.
          </Notice>
        </div>
      )}

      {!rows && <Skeleton className="h-64" />}
      {rows?.length === 0 && <Empty>No cameras onboarded yet.</Empty>}

      {!!rows?.length && (
        <Card>
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Camera</TableHead>
                    <TableHead>What it can do</TableHead>
                    <TableHead>Stream</TableHead>
                    <TableHead>Keyframe</TableHead>
                    <TableHead>Clock</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((c) => {
                    // A keyframe interval above 2s puts a floor under alert latency no matter
                    // how fast the detector is, so it is a column rather than a footnote.
                    const slowKeyframe = (c.gop_ms ?? 0) > 2000
                    return (
                      <TableRow key={c.camera_id}>
                        <TableCell>
                          <div className="font-medium">{c.name}</div>
                          {c.lens !== "rectilinear" && (
                            <Pill tone="warn" className="mt-1">
                              {c.lens}
                            </Pill>
                          )}
                        </TableCell>
                        <TableCell>
                          <Pill tone={GRADE_TONE[c.grade]}>{c.grade}</Pill>
                          <div className="mt-1 text-xs text-muted-foreground">
                            {GRADE_MEANING[c.grade]}
                          </div>
                        </TableCell>
                        <TableCell className="font-mono text-xs text-muted-foreground">
                          {c.resolution ?? "—"}
                          {c.fps ? ` @ ${c.fps}fps` : ""}
                        </TableCell>
                        <TableCell>
                          {c.gop_ms ? (
                            <>
                              <Pill tone={slowKeyframe ? "warn" : "muted"}>{c.gop_ms} ms</Pill>
                              {slowKeyframe && (
                                <div className="mt-1 text-xs text-muted-foreground">
                                  delays alerts
                                </div>
                              )}
                            </>
                          ) : (
                            "—"
                          )}
                        </TableCell>
                        <TableCell>
                          {c.clock_ok === null ? (
                            <Pill tone="muted">not checked</Pill>
                          ) : c.clock_ok ? (
                            <Pill tone="ok">ok</Pill>
                          ) : (
                            <Pill tone="critical">
                              {Math.round((c.clock_offset_ms ?? 0) / 1000)}s off
                            </Pill>
                          )}
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </div>
          </CardContent>
        </Card>
      )}
    </>
  )
}
