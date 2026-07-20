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
