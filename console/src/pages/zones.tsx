import { useCallback, useEffect, useRef, useState } from "react"
import { Loader2, Trash2, Undo2 } from "lucide-react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Empty, PageHeader, Pill } from "@/components/bits"
import { api, type Camera, type Zone } from "@/lib/api"

const PALETTE = ["#3b82f6", "#f97316", "#10b981", "#a855f7", "#ef4444", "#eab308"]

export function ZonesPage() {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [cameras, setCameras] = useState<Camera[]>([])
  const [cameraId, setCameraId] = useState<string>("")
  const [zones, setZones] = useState<Zone[]>([])
  const [drawing, setDrawing] = useState<number[][]>([])
  const [name, setName] = useState("")
  const [kind, setKind] = useState<Zone["kind"]>("area")
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    api.cameras().then((c) => {
      setCameras(c)
      if (c.length) setCameraId(c[0].camera_id)
    })
  }, [])

  useEffect(() => {
    if (!cameraId) return
    setDrawing([])
    api.zones(cameraId).then(setZones)
  }, [cameraId])

  const draw = useCallback(() => {
    const cv = canvasRef.current
    if (!cv) return
    const ctx = cv.getContext("2d")
    if (!ctx) return
    const { width: w, height: h } = cv

    // Placeholder frame. A real deployment paints the camera's latest keyframe here; the grid
    // exists so the editor is usable before any camera is connected, and it is deliberately not
    // a fake image — a plausible fake frame in a demo is worse than an honest gap.
    ctx.fillStyle = "#16181c"
    ctx.fillRect(0, 0, w, h)
    ctx.strokeStyle = "rgba(255,255,255,.05)"
    ctx.lineWidth = 1
    for (let x = 0; x < w; x += 48) {
      ctx.beginPath()
      ctx.moveTo(x, 0)
      ctx.lineTo(x, h)
      ctx.stroke()
    }
    for (let y = 0; y < h; y += 48) {
      ctx.beginPath()
      ctx.moveTo(0, y)
      ctx.lineTo(w, y)
      ctx.stroke()
    }
    ctx.fillStyle = "rgba(255,255,255,.22)"
    ctx.font = "12px ui-monospace, monospace"
    ctx.fillText("live frame appears here once the camera is connected", 16, h - 16)

    const paint = (pts: number[][], colour: string, label: string, k: Zone["kind"], live = false) => {
      if (!pts.length) return
      const px = pts.map(([x, y]) => [x * w, y * h])
      ctx.lineWidth = 2
      ctx.strokeStyle = colour
      ctx.setLineDash(live ? [6, 4] : [])
      ctx.beginPath()
      ctx.moveTo(px[0][0], px[0][1])
      px.slice(1).forEach(([x, y]) => ctx.lineTo(x, y))
      if (k !== "line" && !live) ctx.closePath()
      ctx.stroke()
      if (k !== "line") {
        ctx.fillStyle = colour + "26"
        ctx.fill()
      }
      ctx.setLineDash([])
      px.forEach(([x, y]) => {
        ctx.beginPath()
        ctx.arc(x, y, 4, 0, Math.PI * 2)
        ctx.fillStyle = colour
        ctx.fill()
      })
      if (label) {
        ctx.fillStyle = colour
        ctx.font = "600 13px ui-sans-serif, system-ui"
        ctx.fillText(label, px[0][0] + 8, px[0][1] - 8)
      }
    }

    zones.forEach((z, i) => paint(z.polygon, PALETTE[i % PALETTE.length], z.name, z.kind))
    if (drawing.length) paint(drawing, "#e5e7eb", "", kind, true)
  }, [zones, drawing, kind])

  useEffect(() => {
    draw()
  }, [draw])

  const minPoints = kind === "line" ? 2 : 3
  const ready = drawing.length >= minPoints && name.trim().length > 0

  // Enter is a shortcut, never the only way through. Requiring a keystroke to finish a shape
  // fails silently whenever focus is somewhere unexpected.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement
      if (el.matches("input, textarea, select, [role=combobox]")) return
      if (e.key === "Enter" && ready) {
        e.preventDefault()
        commit()
      }
      if (e.key === "Backspace" && drawing.length) {
        e.preventDefault()
        setDrawing((d) => d.slice(0, -1))
      }
      if (e.key === "Escape") setDrawing([])
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  })

  function commit() {
    if (!ready) return
    setZones((z) => [...z, { name: name.trim(), kind, polygon: drawing, direction: null }])
    setDrawing([])
    setName("")
  }

  async function save() {
    setSaving(true)
    try {
      await api.saveZones(cameraId, zones)
      toast.success("Zones saved", { description: "Rules can now refer to these by name." })
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const hint = !drawing.length
    ? "Click on the frame to place points."
    : drawing.length < minPoints
      ? `${drawing.length} of ${minPoints} points placed.`
      : !name.trim()
        ? `${drawing.length} points placed — now give the zone a name.`
        : `${drawing.length} points placed. Add the shape, or press Enter.`

  return (
    <>
      <PageHeader title="Zones">
        Draw the areas and lines that rules refer to. Coordinates are stored relative to the
        frame, so a zone keeps its meaning if the camera's resolution changes.
      </PageHeader>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_300px]">
        <div className="space-y-3">
          <Select value={cameraId} onValueChange={setCameraId}>
            <SelectTrigger className="w-full max-w-sm">
              <SelectValue placeholder="Choose a camera" />
            </SelectTrigger>
            <SelectContent>
              {cameras.map((c) => (
                <SelectItem key={c.camera_id} value={c.camera_id}>
                  {c.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <canvas
            ref={canvasRef}
            width={960}
            height={540}
            onClick={(e) => {
              const r = e.currentTarget.getBoundingClientRect()
              // Normalised 0-1: a zone stored in pixels silently moves the moment a camera's
              // resolution changes.
              setDrawing((d) => [
                ...d,
                [(e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height],
              ])
            }}
            className="w-full cursor-crosshair rounded-lg border"
          />
          <p className="text-xs text-muted-foreground">
            {hint} <span className="opacity-60">Backspace undoes a point, Esc cancels.</span>
          </p>
        </div>

        <div className="space-y-3">
          <div className="space-y-2">
            <label className="text-xs font-medium text-muted-foreground">Zone name</label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Chemical store"
            />
          </div>

          <div className="space-y-2">
            <label className="text-xs font-medium text-muted-foreground">Type</label>
            <Select value={kind} onValueChange={(v) => setKind(v as Zone["kind"])}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="area">Area — things inside it</SelectItem>
                <SelectItem value="line">Line — things crossing it</SelectItem>
                <SelectItem value="door">Door</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="flex gap-2">
            <Button className="flex-1" variant="outline" onClick={commit} disabled={!ready}>
              Add this shape
            </Button>
            <Button
              variant="ghost"
              size="icon"
              disabled={!drawing.length}
              onClick={() => setDrawing((d) => d.slice(0, -1))}
              title="Undo last point"
            >
              <Undo2 className="size-4" />
            </Button>
          </div>

          <Card>
            <CardContent className="space-y-1.5 p-3">
              {zones.length === 0 ? (
                <p className="py-4 text-center text-xs text-muted-foreground">
                  No zones on this camera yet.
                </p>
              ) : (
                zones.map((z, i) => (
                  <div
                    key={`${z.name}-${i}`}
                    className="flex items-center gap-2 rounded-md border px-2 py-1.5 text-sm"
                  >
                    <span
                      className="size-3 shrink-0 rounded-sm"
                      style={{ background: PALETTE[i % PALETTE.length] }}
                    />
                    <span className="min-w-0 flex-1 truncate">{z.name}</span>
                    <Pill tone="muted">{z.kind}</Pill>
                    <Button
                      size="icon"
                      variant="ghost"
                      className="size-6"
                      onClick={() => setZones((zs) => zs.filter((_, j) => j !== i))}
                    >
                      <Trash2 className="size-3" />
                    </Button>
                  </div>
                ))
              )}
            </CardContent>
          </Card>

          <Button className="w-full" onClick={save} disabled={saving || !cameraId}>
            {saving && <Loader2 className="size-4 animate-spin" />}
            Save zones
          </Button>
        </div>
      </div>

      {cameras.length === 0 && <Empty>No cameras onboarded yet.</Empty>}
    </>
  )
}
