import type {
  ApplicationRecord, ApplicationStats, Connection, Criteria, Posting,
  PostingDetail, Profile, Status, WatchlistCompany,
  AccountStatus, ApplicationRecord, ConnectionsPayload, ContextFile, Criteria,
  EEOStatus, Posting, PostingDetail, Profile, ProfilesPayload, Status,
  TokenVerifyResult, WatchlistCompany,
} from './types'

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path)
  if (!r.ok) throw new Error(`${path}: ${r.status} ${await r.text()}`)
  return r.json()
}

async function send<T>(path: string, method: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!r.ok) {
    let detail = ''
    try { detail = (await r.json()).detail } catch { /* non-JSON error body */ }
    throw new Error(detail || `${path}: ${r.status}`)
  }
  return r.json()
}

export const fetchStatus = () => get<Status>('/api/status')
export const fetchPostings = () =>
  get<{
    postings: Posting[]; last_refresh: string | null; note?: string
    hidden_by_criteria?: number
  }>('/api/postings')
export const fetchApplications = () =>
  get<{ applications: ApplicationRecord[]; stats?: ApplicationStats }>('/api/applications')
export const fetchProfile = () => get<Profile>('/api/profile')
export const fetchWatchlist = () => get<{ companies: WatchlistCompany[] }>('/api/watchlist')
export const fetchConnections = () => get<ConnectionsPayload>('/api/connections')

/* profiles (multi-user) */
export const fetchProfiles = () => get<ProfilesPayload>('/api/profiles')
export const activateProfile = (id: string) =>
  send<ProfilesPayload>('/api/profiles/activate', 'POST', { id })
export const createProfile = (name: string) =>
  send<ProfilesPayload>('/api/profiles', 'POST', { name })

/* EEO self-identification — statuses only; values never come back */
export const fetchEEO = () => get<EEOStatus>('/api/eeo')
export const saveEEO = (values: Record<string, string>) =>
  send<EEOStatus>('/api/eeo', 'PUT', { values })
export const removeEEO = () => send<EEOStatus>('/api/eeo', 'DELETE')

/* cloud account (M2 — local-only until the hosted backend exists) */
export const fetchAccount = () => get<AccountStatus>('/api/account')
export const verifyToken = (token: string) =>
  send<TokenVerifyResult>('/api/account/verify', 'POST', { token })

/* bug reports — local JSONL the daily dev-loop agent triages */
export const reportBug = (text: string, page: string) =>
  send<{ saved: boolean }>('/api/bug-report', 'POST', { text, page })

/* knowledge base paste + remove (wizard step 4) */
export const pasteContext = (text: string, kind: 'pasted' | 'story', title = '') =>
  send<{ saved: string; context_files: ContextFile[] }>(
    '/api/context/paste', 'POST', { text, kind, title })
export const deleteContext = (name: string) =>
  send<{ removed: string; context_files: ContextFile[] }>(
    `/api/context/${encodeURIComponent(name)}`, 'DELETE')

// Records the company's response on one application (JOB-107). `key` is the
// opaque identity the GET stamps on each row — never construct one client-side.
export const setApplicationOutcome = (key: string, outcome: string, outcome_date = '') =>
  send<{ application: ApplicationRecord; stats: ApplicationStats }>(
    '/api/applications/outcome', 'POST', { key, outcome, outcome_date })

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
