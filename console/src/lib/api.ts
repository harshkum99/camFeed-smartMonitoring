/** Typed client for the Smart Cam Monitoring API.
 *
 *  Every call goes through `request` so that error handling is uniform: the API answers a
 *  *refusal* with 200 and a reason, and reserves non-2xx for things that actually went wrong.
 *  Collapsing those two would make "nothing happened" and "we could not look" identical to the
 *  UI, which is the failure the whole product is built to avoid.
 */

const HEADERS = {
  "content-type": "application/json",
  "x-smartcam-actor": "console",
}

/** Where the API lives.
 *
 *  Empty by default: when FastAPI serves the built console, both come off the same origin and
 *  relative paths are correct. Set `VITE_API_BASE` at build time when the console is hosted
 *  separately (a CDN, a preview deploy) and the API is not — the backend needs a database and a
 *  long-lived connection, so it stays on a real server rather than following the static files.
 */
export const API_BASE = (import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "")

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: HEADERS,
    // Only meaningful cross-origin; harmless same-origin.
    credentials: API_BASE ? "include" : "same-origin",
    ...init,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      detail = (await res.json()).detail ?? detail
    } catch {
      /* error body was not JSON */
    }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

/* ---------- types ---------- */

export type Grade = "recognition" | "anpr" | "detection" | "degraded" | "unservable"
export type Severity = "info" | "warn" | "critical"
export type AbstainReason = "no_coverage" | "no_evidence" | "not_measured" | "low_confidence"

export interface Camera {
  camera_id: string
  name: string
  grade: Grade
  lens: string
  resolution: string | null
  fps: number | null
  gop_ms: number | null
  clock_offset_ms: number | null
  /** null when the clock was never checked — a recording has no live clock to check. */
  clock_ok: boolean | null
  capabilities: Record<string, CapabilityEntry>
  grade_notes: string[]
  graded_from: string | null
}

export type CapabilityStatus = "reliable" | "limited" | "unreliable" | "not_assessed"

export interface CapabilityEntry {
  status: CapabilityStatus
  frame_recall?: number | null
  basis?: string
}

export interface Site {
  site_id: string
  name: string
  tz: string
  as_of: string | null
  replay: boolean
  attribution: string | null
  examples: string[]
}

export interface Coverage {
  coverage_pct: number
  cameras: number
  hours: number
  gaps: { camera: string; from: string; to: string; minutes: number; recorded?: boolean }[]
  as_of?: string | null
}

export interface Evidence {
  track_id: string
  camera: string
  /** When the frame shown was captured. */
  ts: string
  track_start: string | null
  confidence: number
  /** Served by the API, scoped to this deployment's site; null when no frame is held. */
  keyframe_url: string | null
  frame_sha256: string | null
  /** Normalised x1, y1, x2, y2 of the subject in the frame. */
  bbox: number[] | null
  attrs: Record<string, number | string>
}

export interface CameraLimit {
  camera: string
  class: string
  status: "unreliable" | "limited" | "not_assessed" | "absent"
  frame_recall: number | null
  message: string
}

export interface Answer {
  question: string
  message: string
  refused: boolean
  abstain_reason: AbstainReason | null
  describes: string | null
  confident: number
  ambiguous: number
  total: number
  coverage_pct: number
  gaps: { camera: string; minutes: number; seconds: number; recorded: boolean }[]
  limits: CameraLimit[]
  notes: string[]
  unresolved: string[]
  as_of: string | null
  site_tz: string
  evidence: Evidence[]
  latency_ms: number
}

export interface Zone {
  zone_id?: string
  name: string
  kind: "area" | "line" | "door"
  polygon: number[][]
  direction?: string | null
}

export interface RuleDraft {
  ok: boolean
  reason?: string
  explain?: string
  rule?: Record<string, unknown>
  notes?: string[]
  unresolved?: string[]
  min_alert_latency_s?: number
  needs_verification?: boolean
}

export interface RuleRow {
  rule_id: string
  name: string
  severity: Severity
  enabled: boolean
  trigger: Record<string, unknown>
  true_positives: number
  false_positives: number
  precision: number | null
}

export interface AlertRow {
  alert_id: string
  ts: string
  severity: Severity
  state: string
  camera: string
  rule: string | null
  verified: boolean | null
  evidence: string[]
  feedback: string | null
}

export interface AuditRow {
  at: string
  actor: string
  question: string
  refused: boolean
  reason: AbstainReason | null
  coverage_pct: number | null
  rows: number | null
  latency_ms: number | null
}

export interface BundleSummary {
  bundle_id: string
  created_at: string
  requested_by: string
  purpose: string
  question: string | null
  window: { from: string; to: string }
  cameras: number
  frames: number
  tracks: number
  sources: { file: string; sha256: string; included: boolean }[]
  merkle_root: string
  manifest_sha256: string
  archive_sha256: string
  certificate: boolean
  clock_warnings: string[]
  warnings?: string[]
}

export interface CustodyEntry {
  at: string
  actor: string
  action: string
  detail: Record<string, unknown>
}

export interface BundleDetail extends BundleSummary {
  custody: CustodyEntry[]
}

/* ---------- calls ---------- */

export const api = {
  health: () => request<{ ok: boolean; interpreter: string }>("/api/health"),
  site: () => request<Site>("/api/site"),
  cameras: () => request<Camera[]>("/api/cameras"),
  coverage: (hours = 24) => request<Coverage>(`/api/coverage?hours=${hours}`),
  activity: (hours = 48) => request<ActivityPoint[]>(`/api/activity?hours=${hours}`),

  ask: (question: string) =>
    request<Answer>("/api/ask", { method: "POST", body: JSON.stringify({ question }) }),

  zones: (cameraId: string) => request<Zone[]>(`/api/cameras/${cameraId}/zones`),
  saveZones: (cameraId: string, zones: Zone[]) =>
    request<{ saved: number }>(`/api/cameras/${cameraId}/zones`, {
      method: "PUT",
      body: JSON.stringify(
        zones.map((z) => ({ name: z.name, kind: z.kind, polygon: z.polygon, direction: z.direction })),
      ),
    }),

  draftRule: (instruction: string) =>
    request<RuleDraft>("/api/rules/draft", {
      method: "POST",
      body: JSON.stringify({ instruction }),
    }),
  saveRule: (rule: Record<string, unknown>) =>
    request<{ rule_id: string; name: string; notes: string[] }>("/api/rules", {
      method: "POST",
      body: JSON.stringify(rule),
    }),
  rules: () => request<RuleRow[]>("/api/rules"),
  disableRule: (id: string) => request<unknown>(`/api/rules/${id}`, { method: "DELETE" }),

  alerts: (limit = 50) => request<AlertRow[]>(`/api/alerts?limit=${limit}`),
  alertFeedback: (id: string, actioned: boolean) =>
    request<{ recorded: string; rule_muted: boolean; message: string }>(
      `/api/alerts/${id}/feedback`,
      { method: "POST", body: JSON.stringify({ actioned }) },
    ),

  audit: (limit = 60) => request<AuditRow[]>(`/api/audit?limit=${limit}`),

  createBundle: (track_ids: string[], purpose: string, question?: string) =>
    request<BundleSummary>("/api/evidence/bundles", {
      method: "POST",
      body: JSON.stringify({ track_ids, purpose, question }),
    }),
  bundles: () => request<BundleSummary[]>("/api/evidence/bundles"),
  bundle: (id: string) => request<BundleDetail>(`/api/evidence/bundles/${id}`),
  bundleArchiveUrl: (id: string) => `${API_BASE}/api/evidence/bundles/${id}/archive`,
  bundleCertificateUrl: (id: string) => `${API_BASE}/api/evidence/bundles/${id}/certificate`,
  /** Issues the next numbered draft for a new submission. A POST, because it uses up a number
   *  and writes to the custody log; the PDF comes back as a blob for the browser to open. */
  newDraft: async (id: string): Promise<{ blob: Blob; draftId: string }> => {
    const res = await fetch(`${API_BASE}/api/evidence/bundles/${id}/drafts`, {
      method: "POST",
      headers: HEADERS,
      credentials: API_BASE ? "include" : "same-origin",
    })
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? res.statusText)
    return { blob: await res.blob(), draftId: res.headers.get("x-draft-id") ?? "" }
  },
}

/* ---------- shared presentation vocabulary ----------
 * Kept here rather than in each page so a grade or a refusal never means two different things
 * in two different places. */

export const GRADE_MEANING: Record<Grade, string> = {
  recognition: "Can identify enrolled faces",
  anpr: "Can read number plates",
  detection: "People and zones",
  degraded: "Works, but costs about twice the appliance capacity",
  unservable: "Cannot be used as configured",
}

export const REFUSAL_LABEL: Record<AbstainReason, string> = {
  no_coverage: "No coverage",
  no_evidence: "No evidence",
  not_measured: "Not measured",
  low_confidence: "Low confidence",
}

export const REFUSAL_HELP: Record<AbstainReason, string> = {
  no_coverage:
    "We hold no usable footage for part of this period, so we cannot tell you nothing happened.",
  no_evidence: "We looked, coverage was good, and there was genuinely nothing.",
  not_measured: "No detector was running for this, so the answer is unknown rather than zero.",
  low_confidence: "There is something, but it is too weak to assert. Have a look.",
}

export interface ActivityPoint {
  hour: string
  events: number
}
