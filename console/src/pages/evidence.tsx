import { useEffect, useRef, useState } from "react"
import { toast } from "sonner"
import { Download, FileText } from "lucide-react"
import { Button } from "@/components/ui/button"
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
import { api, type BundleDetail, type BundleSummary } from "@/lib/api"
import { formatSiteTime, useSite } from "@/lib/site"

export function EvidencePage() {
  const site = useSite()
  const [rows, setRows] = useState<BundleSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState<BundleDetail | null>(null)

  useEffect(() => {
    api.bundles().then(setRows).catch((e) => setError((e as Error).message))
  }, [])

  // Only the most recently requested bundle may fill the panel: a slower response for a row
  // clicked earlier must not show one bundle's hashes under another's name.
  const latest = useRef<string | null>(null)
  const show = (id: string) => {
    latest.current = id
    api
      .bundle(id)
      .then((b) => latest.current === id && setOpen(b))
      .catch((e) => setError((e as Error).message))
  }
  const refresh = (id: string) => window.setTimeout(() => latest.current === id && show(id), 800)

  async function newDraft(id: string) {
    try {
      const { blob, draftId } = await api.newDraft(id)
      window.open(URL.createObjectURL(blob), "_blank", "noopener")
      toast.success(`Draft ${draftId} issued and logged`)
      if (latest.current === id) show(id)
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  return (
    <>
      <PageHeader title="Evidence">
        Bundles prepared for certification under section 63 of the Bharatiya Sakshya Adhiniyam.
        Each holds the original recordings, checked against the hash taken on import, with a hash
        report, a Merkle root anyone can recompute, and a draft certificate for the person in
        charge of the recorder and an expert to sign. A bundle cannot be edited once made; every download is recorded.
      </PageHeader>

      {error && (
        <Notice tone="critical" title="Could not load evidence bundles">
          {error}
        </Notice>
      )}
      {!rows && !error && <Skeleton className="h-48" />}
      {rows?.length === 0 && (
        <Empty>No bundles yet. Ask a question, then prepare a bundle from its evidence.</Empty>
      )}

      {!!rows?.length && (
        <Card className="mb-5">
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Prepared (IST)</TableHead>
                    <TableHead>Purpose</TableHead>
                    <TableHead>Recordings</TableHead>
                    <TableHead>Merkle root</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((b) => (
                    <TableRow key={b.bundle_id} className="cursor-pointer" onClick={() => show(b.bundle_id)}>
                      <TableCell className="whitespace-nowrap font-mono text-xs">
                        {formatSiteTime(b.created_at, "Asia/Kolkata", { date: true })}
                        <div className="text-muted-foreground">{b.requested_by}</div>
                      </TableCell>
                      <TableCell className="max-w-md text-sm">{b.purpose}</TableCell>
                      <TableCell className="text-sm">
                        {b.sources.filter((s) => s.included).length} of {b.sources.length}
                        {b.clock_warnings.length > 0 && (
                          <Pill tone="warn" className="ml-2">clock unchecked</Pill>
                        )}
                      </TableCell>
                      <TableCell className="font-mono text-xs">{b.merkle_root.slice(0, 16)}…</TableCell>
                      <TableCell className="whitespace-nowrap text-right">
                        <Button
                          size="sm"
                          variant="ghost"
                          title="A fresh, numbered draft for a new submission — each is logged"
                          onClick={(e) => {
                            e.stopPropagation()
                            newDraft(b.bundle_id)
                          }}
                        >
                          <FileText className="size-4" /> New draft
                        </Button>
                        <Button
                          asChild
                          size="sm"
                          variant="ghost"
                          onClick={(e) => {
                            e.stopPropagation()
                            refresh(b.bundle_id)
                          }}
                        >
                          <a href={api.bundleArchiveUrl(b.bundle_id)}>
                            <Download className="size-4" /> Bundle
                          </a>
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </CardContent>
        </Card>
      )}

      {open && <BundlePanel b={open} tz={site?.tz} />}
    </>
  )
}

function BundlePanel({ b, tz }: { b: BundleDetail; tz?: string }) {
  return (
    <Card>
      <CardContent className="space-y-4 p-5 text-sm">
        <div>
          <div className="font-medium">{b.purpose}</div>
          {b.question && <div className="text-muted-foreground">From the question: {b.question}</div>}
          <div className="mt-1 text-xs text-muted-foreground">
            Footage {formatSiteTime(b.window.from, tz, { date: true })} to{" "}
            {formatSiteTime(b.window.to, tz)}
          </div>
        </div>
        <dl className="grid grid-cols-[10rem_1fr] gap-x-3 gap-y-1 font-mono text-xs">
          <dt className="text-muted-foreground">merkle root</dt>
          <dd className="break-all">{b.merkle_root}</dd>
          <dt className="text-muted-foreground">manifest sha-256</dt>
          <dd className="break-all">{b.manifest_sha256}</dd>
          <dt className="text-muted-foreground">archive sha-256</dt>
          <dd className="break-all">{b.archive_sha256}</dd>
        </dl>
        <div>
          <div className="mb-1 font-medium">Original recordings</div>
          {b.sources.map((s) => (
            <div key={s.sha256} className="font-mono text-xs">
              {s.included ? "✓" : "✗"} {s.file}
              <div className="break-all pl-4 text-muted-foreground">{s.sha256}</div>
            </div>
          ))}
        </div>
        {b.clock_warnings.map((w) => (
          <Notice key={w} tone="warn">{w}</Notice>
        ))}
        <div>
          <div className="mb-1 font-medium">Chain of custody (IST)</div>
          <div className="space-y-0.5 font-mono text-xs">
            {b.custody.map((c, i) => (
              <div key={i}>
                {formatSiteTime(c.at, "Asia/Kolkata", { date: true, seconds: true })} · {c.actor} ·{" "}
                <span className="font-medium">{c.action}</span>
              </div>
            ))}
          </div>
        </div>
      </CardContent>
    </Card>
  )
}
