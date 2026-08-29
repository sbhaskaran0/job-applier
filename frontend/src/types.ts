export type Page = 'chat' | 'postings' | 'applications' | 'profile' | 'connections'

export interface Posting {
  company: string
  title: string
  location: string
  locations: string[]
  work_mode: 'remote' | 'hybrid' | 'onsite'
  remote: boolean
  salary_min: number | null
  salary_max: number | null
  salary_listed: boolean
  min_years: number | null
  url: string
  first_seen: string
  posted_at: string | null
  is_new: boolean
  already_applied: boolean
  ats: string
  snippet: string
}

export interface PostingDetail {
  url: string
  found: boolean
  source: 'store' | 'live'
  description?: string
  title?: string
  note?: string
}

export interface Criteria {
  titles: string[]
  search_titles: string[]
  locations: string[]
  acceptable_seniority: string[]
  excluded_seniority: string[]
  salary_floor: number | null
  date_posted_days: number | null
  remote_ok: boolean
  yoe: [number, number]
}

export interface ApplicationRecord {
  company: string
  job_title: string
  url: string
  date: string
  status: 'submitted' | 'manual_submission' | 'attempted' | 'parked' | string
  fields?: { question: string; answer: string }[]
  // JOB-107: orthogonal to `status` — did the company ever come back? Optional
  // because records logged before outcomes existed carry neither field; every
  // reader defaults a missing outcome to 'none'.
  outcome?: string
  outcome_date?: string
  // Opaque row identity stamped by the API; the target of a POST /applications/outcome.
  key?: string
}

export interface ApplicationCompanyStats {
  submitted: number
  responded: number
  by_outcome: Record<string, number>
}

export interface ApplicationStats {
  total: number
  submitted: number
  responded: number
  // responded / submitted as a fraction. 0.0 is a real answer, not "no data".
  response_rate: number
  by_outcome: Record<string, number>
  by_company: Record<string, ApplicationCompanyStats>
}

export interface WatchlistCompany {
  name: string
  ats: string
  slug: string
  active: number
  qualifying: number
}

export interface Connection {
  id: string
  name: string
  mono: string
  required: boolean
  connected: boolean
  short: string
  desc: string
}

export interface ConnectionsPayload {
  connections: Connection[]
  gmail_account: string | null
  note: string
}

export interface ContextFile {
  name: string
  kind: string
  size: number
}

export interface Profile {
  facts: Record<string, string>
  eeo_fields_present: string[]
  completeness: number
  resume_pdf: boolean
  resume_docx: boolean
  context_files: ContextFile[]
  profile_id: string
  profile_dir: string
  created: string
}

export interface ProfileSummary {
  id: string
  name: string
  applications: number
  completeness: number
  resume: boolean
  last_used: string | null
  created: string | null
  active: boolean
}

export interface ProfilesPayload {
  profiles: ProfileSummary[]
  active_id: string | null
}

export type EEOFieldStatus = 'set' | 'prefer_not' | 'not_set'

export interface EEOStatus {
  present: boolean
  fields: { key: string; label: string; status: EEOFieldStatus }[]
}

export interface AccountDevice {
  label: string
  created: string
  last_used: string
  current: boolean
}

export interface AccountStatus {
  linked: boolean
  email?: string
  provider?: string
  offline?: boolean
  last_synced?: string
  devices?: AccountDevice[]
  note?: string
}

export interface TokenVerifyResult {
  ok: boolean
  reason?: 'invalid' | 'unreachable' | string
  detail?: string
  email?: string
  device_label?: string
}

export interface Status {
  last_refresh: string | null
  store_age_hours: number | null
  new_qualifying: number
  watchlist_count: number
}

/* Chat */
export interface RunStep {
  label: string
  status: 'done' | 'active' | 'warn' | 'pending'
}

export interface RunCard {
  chip: string
  title: string
  steps: RunStep[]
  footer?: string
}

export interface ChatMessage {
  role: 'user' | 'agent'
  text?: string
  run?: RunCard
}
