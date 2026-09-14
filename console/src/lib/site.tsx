/** The site being viewed, and how to print a time for it.
 *
 *  Every timestamp in this product is a claim about when something happened at a place, so it is
 *  printed in that place's timezone with its date — never in the viewer's browser timezone. An
 *  operator in Pune looking at footage recorded at 11:55 in Indiana must read 11:55 EDT, not
 *  21:25, or the answer and the evidence frame appear to disagree about the time.
 */

import { createContext, useContext, useEffect, useState } from "react"
import { api, type Site } from "@/lib/api"

const SiteContext = createContext<Site | null>(null)

export function SiteProvider({ children }: { children: React.ReactNode }) {
  const [site, setSite] = useState<Site | null>(null)
  useEffect(() => {
    // Retry until it answers. Without the site, a recording is shown with no replay banner and no
    // licence attribution, and every relative time reads as live.
    let cancelled = false
    let delay = 1000
    const load = () => {
      api
        .site()
        .then((s) => !cancelled && setSite(s))
        .catch(() => {
          if (cancelled) return
          setTimeout(load, delay)
          delay = Math.min(delay * 2, 30_000)
        })
    }
    load()
    return () => {
      cancelled = true
    }
  }, [])
  return <SiteContext.Provider value={site}>{children}</SiteContext.Provider>
}

export function useSite(): Site | null {
  return useContext(SiteContext)
}

export function formatSiteTime(
  ts: string | Date,
  tz: string | undefined,
  opts: { date?: boolean; seconds?: boolean } = {},
): string {
  const d = typeof ts === "string" ? new Date(ts) : ts
  try {
    return format(d, tz, opts)
  } catch {
    // An unrecognised zone must not blank the console, and must not silently fall back to the
    // viewer's own zone either — UTC, labelled as such.
    return format(d, "UTC", opts)
  }
}

function format(d: Date, tz: string | undefined, opts: { date?: boolean; seconds?: boolean }) {
  return new Intl.DateTimeFormat(undefined, {
    timeZone: tz,
    ...(opts.date ? { day: "numeric", month: "short", year: "numeric" } : {}),
    hour: "2-digit",
    minute: "2-digit",
    ...(opts.seconds ? { second: "2-digit" } : {}),
    hour12: false,
    timeZoneName: "short",
  }).format(d)
}
