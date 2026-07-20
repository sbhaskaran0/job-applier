import type {
  ApplicationRecord, Connection, Criteria, Posting, PostingDetail, Profile,
  Status, WatchlistCompany,
} from './types'

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path)
  if (!r.ok) throw new Error(`${path}: ${r.status} ${await r.text()}`)
  return r.json()
}

export const fetchStatus = () => get<Status>('/api/status')
export const fetchPostings = () =>
  get<{ postings: Posting[]; last_refresh: string | null; note?: string }>('/api/postings')
export const fetchApplications = () =>
  get<{ applications: ApplicationRecord[] }>('/api/applications')
export const fetchProfile = () => get<Profile>('/api/profile')
export const fetchWatchlist = () => get<{ companies: WatchlistCompany[] }>('/api/watchlist')
export const fetchConnections = () =>
  get<{ connections: Connection[]; note: string }>('/api/connections')

export async function addWatchlistCompany(url: string) {
  const r = await fetch('/api/watchlist', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  })
  if (!r.ok) throw new Error((await r.json()).detail ?? 'could not add company')
  return r.json() as Promise<{ status: string; name?: string; ats?: string; slug?: string }>
}

export async function refreshPostings() {
  const r = await fetch('/api/refresh', { method: 'POST' })
  if (!r.ok) throw new Error((await r.json()).detail ?? 'refresh failed')
  return r.json() as Promise<{
    run_at: string
    total_scanned: number
    new_count: number
    removed_count: number
    relisted_count: number
    boards_failed: string[]
  }>
}

export const fetchCriteria = () => get<Criteria>('/api/criteria')
export const fetchPostingDetail = (url: string) =>
  get<PostingDetail>(`/api/posting?url=${encodeURIComponent(url)}`)

export async function updateCriteria(update: Partial<Criteria>) {
  const r = await fetch('/api/criteria', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(update),
  })
  if (!r.ok) throw new Error((await r.json()).detail ?? 'could not save criteria')
  return r.json() as Promise<Criteria>
}

export async function updateProfile(facts: Record<string, string>) {
  const r = await fetch('/api/profile', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ facts }),
  })
  if (!r.ok) throw new Error((await r.json()).detail ?? 'could not save profile')
  return r.json() as Promise<Profile>
}

export async function uploadFile(kind: 'resume' | 'context', file: File) {
  const form = new FormData()
  form.append('file', file)
  const r = await fetch(`/api/upload/${kind}`, { method: 'POST', body: form })
  if (!r.ok) throw new Error((await r.json()).detail ?? 'upload failed')
  return r.json()
}

export function formatSalary(p: Posting): string {
  if (!p.salary_listed || p.salary_min == null) return 'not listed'
  const k = (n: number) => `$${Math.round(n / 1000)}`
  if (p.salary_max != null && p.salary_max !== p.salary_min)
    return `${k(p.salary_min)}–${Math.round(p.salary_max / 1000)}k`
  return `${k(p.salary_min)}k`
}

export function formatYears(p: Posting): string {
  return p.min_years ? `${p.min_years}+ yrs` : '—'
}

export function monogram(name: string): string {
  return name.slice(0, 2)
}

export function agoHours(h: number | null): string {
  if (h == null) return 'never'
  if (h < 1) return `${Math.max(1, Math.round(h * 60))}m ago`
  if (h < 48) return `${Math.round(h)}h ago`
  return `${Math.round(h / 24)}d ago`
}
