import { useEffect, useState } from 'react'
import { fetchCriteria, formatSalary, formatYears, monogram, refreshPostings } from '../api'
import type { Posting } from '../types'
import JobDetailModal from './JobDetailModal'
import MultiSelect from './MultiSelect'
import RangeSlider from './RangeSlider'
import Toggle from './Toggle'

const YOE_RANGE: [number, number] = [0, 15]
const SALARY_RANGE: [number, number] = [60, 400] // $ thousands

const COMMON_METROS = [
  'Los Angeles', 'San Francisco', 'New York', 'Remote', 'Remote · US',
  'Austin', 'Seattle', 'Boston',
]

const DATE_PRESETS = [
  ['any', 'Any time'],
  ['1', 'Past 24h'],
  ['7', 'Past 7 days'],
  ['30', 'Past 30 days'],
] as const

type DatePreset = (typeof DATE_PRESETS)[number][0] | 'custom'

const FIELD_LABEL: React.CSSProperties = {
  fontSize: 11, letterSpacing: '.05em', textTransform: 'uppercase',
  color: 'var(--text-5)', marginBottom: 7,
}

interface Props {
  postings: Posting[]
  note?: string
  selected: Set<string>
  setSelected: (s: Set<string>) => void
  autonomous: boolean
  setAutonomous: (v: boolean) => void
  openApply: () => void
  reload: () => void
}

export default function PostingsPage({
  postings, note, selected, setSelected, autonomous, setAutonomous, openApply, reload,
}: Props) {
  const [titles, setTitles] = useState<string[]>([])
  const [locations, setLocations] = useState<string[]>([])
  const [yoe, setYoe] = useState<[number, number]>(YOE_RANGE)
  const [salary, setSalary] = useState<[number, number]>(SALARY_RANGE)
  const [datePreset, setDatePreset] = useState<DatePreset>('any')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [includeMissing, setIncludeMissing] = useState(true)
  const [titleOptions, setTitleOptions] = useState<string[]>([])
  const [locationOptions, setLocationOptions] = useState<string[]>(COMMON_METROS)
  const [detail, setDetail] = useState<Posting | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [refreshNote, setRefreshNote] = useState<{ text: string; error: boolean } | null>(null)

  const runRefresh = async () => {
    if (refreshing) return
    setRefreshing(true)
    setRefreshNote(null)
    try {
      const s = await refreshPostings()
      reload()
      const failed = s.boards_failed.length
        ? ` · ${s.boards_failed.length} board(s) failed` : ''
      setRefreshNote({
        text: `Refreshed — ${s.total_scanned.toLocaleString()} scanned · `
          + `${s.new_count} new · ${s.removed_count} removed${failed}`,
        error: false,
      })
    } catch (e) {
      setRefreshNote({ text: e instanceof Error ? e.message : String(e), error: true })
    } finally {
      setRefreshing(false)
    }
  }

  useEffect(() => {
    fetchCriteria().then((c) => {
      setTitleOptions([...new Set([...c.search_titles, ...c.titles])])
      setLocationOptions([...new Set([...c.locations, ...COMMON_METROS])])
    }).catch(() => setTitleOptions([]))
  }, [])

  const resetFilters = () => {
    setTitles([]); setLocations([])
    setYoe(YOE_RANGE); setSalary(SALARY_RANGE)
    setDatePreset('any'); setDateFrom(''); setDateTo('')
    setIncludeMissing(true)
  }

  const matchesFilters = (p: Posting): boolean => {
    if (!includeMissing && (!p.salary_listed || p.min_years == null)) return false
    if (titles.length
      && !titles.some((t) => p.title.toLowerCase().includes(t.toLowerCase()))) return false
    if (locations.length && !locations.some((sel) => {
      const s = sel.toLowerCase()
      if (/remote/.test(s) && (p.remote || p.work_mode === 'remote')) return true
      return (p.locations ?? []).some((l) => l.toLowerCase().includes(s))
        || p.location.toLowerCase().includes(s)
    })) return false
    // full slider range = filter off, so out-of-range values still show
    if (p.min_years != null
      && (yoe[0] > YOE_RANGE[0] || yoe[1] < YOE_RANGE[1])
      && (p.min_years < yoe[0] || p.min_years > yoe[1])) return false
    if (p.salary_listed && p.salary_min != null
      && (salary[0] > SALARY_RANGE[0] || salary[1] < SALARY_RANGE[1])
      && (p.salary_min < salary[0] * 1000 || p.salary_min > salary[1] * 1000)) return false
    if (datePreset === 'custom') {
      if (p.posted_at == null) return true
      const posted = new Date(p.posted_at).getTime()
      if (dateFrom && posted < new Date(`${dateFrom}T00:00:00`).getTime()) return false
      if (dateTo && posted > new Date(`${dateTo}T23:59:59`).getTime()) return false
    } else if (datePreset !== 'any') {
      if (p.posted_at == null) return true
      const daysAgo = (Date.now() - new Date(p.posted_at).getTime()) / 86_400_000
      if (daysAgo > Number(datePreset)) return false
    }
    return true
  }

  // already-applied roles are excluded outright — they live on the
  // Applications page, not in the apply queue
  const pool = postings.filter((p) => !p.already_applied)
  const visible = pool.filter(matchesFilters)

  const toggle = (url: string) => {
    const next = new Set(selected)
    if (next.has(url)) next.delete(url)
    else next.add(url)
    setSelected(next)
  }

  const pillStyle = (active: boolean): React.CSSProperties => ({
    fontSize: 12.5, fontWeight: 600, padding: '7px 14px', borderRadius: 20,
    cursor: 'pointer', whiteSpace: 'nowrap',
    border: `1px solid ${active ? 'var(--clay)' : 'var(--border-2)'}`,
    background: active ? 'var(--clay)' : 'var(--chip)',
    color: active ? 'var(--on-clay)' : 'var(--text-2)',
  })

  const dateInputStyle: React.CSSProperties = {
    padding: '6px 8px', border: '1px solid var(--border-2)', borderRadius: 8,
    background: 'var(--bg-app)', color: 'var(--ink)', fontSize: 12.5,
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0, position: 'relative' }}>
      <header className="page-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <h1>Postings</h1>
          <button onClick={runRefresh} disabled={refreshing}
            title="Fetch every watchlist board and update the postings store"
            style={{
              display: 'flex', alignItems: 'center', gap: 7, padding: '6px 13px',
              borderRadius: 9, fontSize: 12.5, fontWeight: 600,
              border: '1px solid var(--border-2)', background: 'var(--chip)',
              color: 'var(--text-2)', cursor: refreshing ? 'default' : 'pointer',
              opacity: refreshing ? 0.65 : 1,
            }}>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"
              style={refreshing ? { animation: 'spin 1s linear infinite' } : undefined}>
              <path d="M21 12a9 9 0 1 1-2.64-6.36" />
              <path d="M21 3v6h-6" />
            </svg>
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
        <p>{visible.length} of {pool.length} roles match · select to queue an application</p>
        {refreshing && (
          <p style={{ color: 'var(--text-4)' }}>
            Sweeping the watchlist boards — this takes a minute…
          </p>
        )}
        {refreshNote && (
          <p style={{ color: refreshNote.error ? 'var(--amber-text)' : 'var(--sage-text)' }}>
            {refreshNote.text}
          </p>
        )}
        {note && <p style={{ color: 'var(--amber-text)' }}>{note}</p>}

        <div style={{
          marginTop: 16, background: 'var(--bg-card)', border: '1px solid var(--border-1)',
          borderRadius: 16, padding: '16px 18px', maxWidth: 920,
          boxShadow: '0 1px 2px rgba(80,60,30,0.04)',
        }}>
          <div style={{
            display: 'flex', justifyContent: 'space-between', alignItems: 'center',
            marginBottom: 14,
          }}>
            <span style={{
              display: 'flex', alignItems: 'center', gap: 7,
              fontSize: 12.5, fontWeight: 600, color: 'var(--text-2)',
            }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
              </svg>
              Filter roles
            </span>
            <button onClick={resetFilters} style={{
              fontSize: 12, fontWeight: 600, color: 'var(--clay-text)',
              background: 'none', border: 'none', cursor: 'pointer', padding: 0,
            }}>Reset filters</button>
          </div>

          <div style={{ marginBottom: 16 }}>
            <div style={FIELD_LABEL}>Job title</div>
            <MultiSelect
              placeholder="Search job titles…" options={titleOptions}
              selected={titles} onChange={setTitles}
              chipBg="var(--accent-soft)" chipColor="var(--clay-text)"
            />
          </div>
          <div style={{ marginBottom: 16 }}>
            <div style={FIELD_LABEL}>Location</div>
            <MultiSelect
              placeholder="Search locations…" options={locationOptions}
              selected={locations} onChange={setLocations}
              chipBg="var(--sage-soft)" chipColor="var(--sage-text)"
            />
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px 24px' }}>
            <RangeSlider label="Years of experience" min={YOE_RANGE[0]} max={YOE_RANGE[1]}
              step={1} value={yoe} onChange={setYoe} unit="yrs" />
            <RangeSlider label="Salary range" min={SALARY_RANGE[0]} max={SALARY_RANGE[1]}
              step={5} value={salary} onChange={setSalary} unit="k" />

            <div style={{ gridColumn: '1 / -1' }}>
              <div style={FIELD_LABEL}>Posted date</div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                {DATE_PRESETS.map(([k, label]) => (
                  <button key={k} style={pillStyle(datePreset === k)}
                    onClick={() => { setDatePreset(k); setDateFrom(''); setDateTo('') }}>
                    {label}
                  </button>
                ))}
                <span style={{
                  marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 7,
                  fontSize: 12, color: 'var(--text-4)',
                }}>
                  From
                  <input type="date" value={dateFrom} style={dateInputStyle}
                    onChange={(e) => { setDateFrom(e.target.value); setDatePreset('custom') }} />
                  –
                  To
                  <input type="date" value={dateTo} style={dateInputStyle}
                    onChange={(e) => { setDateTo(e.target.value); setDatePreset('custom') }} />
                </span>
              </div>
            </div>

            <div style={{
              gridColumn: '1 / -1', display: 'flex', alignItems: 'center', gap: 11,
              borderTop: '1px solid var(--divider)', paddingTop: 14,
            }}>
              <Toggle on={includeMissing} onChange={setIncludeMissing} />
              <div>
                <div style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--text-2)' }}>
                  Include jobs with missing data
                </div>
                <div style={{ fontSize: 11.5, color: 'var(--text-4)' }}>
                  Keep roles with no listed salary or unstated experience — otherwise
                  the range filters hide them.
                </div>
              </div>
            </div>
          </div>
        </div>
      </header>

      <div style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', padding: '18px 34px 120px' }}>
        <div style={{ maxWidth: 920 }}>
          {visible.map((p) => {
            const sel = selected.has(p.url)
            return (
              <div key={p.url} onClick={() => toggle(p.url)} style={{
                display: 'flex', alignItems: 'center', gap: 14, padding: '14px 16px',
                marginBottom: 8, borderRadius: 14, cursor: 'pointer',
                border: `1px solid ${sel ? 'var(--clay)' : 'var(--border-1)'}`,
                background: sel ? 'var(--row-selected)' : 'var(--bg-card)',
                boxShadow: '0 1px 2px rgba(80,60,30,0.03)',
              }}>
                <div style={{
                  width: 22, height: 22, borderRadius: 7, flex: 'none', display: 'flex',
                  alignItems: 'center', justifyContent: 'center',
                  border: `2px solid ${sel ? 'var(--clay)' : 'var(--border-4)'}`,
                  background: sel ? 'var(--clay)' : 'transparent',
                }}>
                  {sel && (
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--on-clay)"
                      strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M20 6 9 17l-5-5" />
                    </svg>
                  )}
                </div>
                <div className="mono-tile" style={{ width: 40, height: 40, borderRadius: 10, fontSize: 15 }}>
                  {monogram(p.company)}
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
                    <span style={{ fontSize: 14.5, fontWeight: 600, color: 'var(--ink)' }}>{p.title}</span>
                    <button title="View job description" aria-label={`View job description: ${p.title}`}
                      onClick={(e) => { e.stopPropagation(); setDetail(p) }}
                      style={{
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                        width: 20, height: 20, borderRadius: 6, flex: 'none', padding: 0,
                        border: '1px solid var(--border-2)', background: 'var(--chip)',
                        color: 'var(--text-3)', cursor: 'pointer',
                      }}>
                      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                        strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                        <path d="m9 18 6-6-6-6" />
                      </svg>
                    </button>
                    {p.is_new && (
                      <span className="tag" style={{ color: 'var(--sage-text)', background: 'var(--sage-soft)' }}>NEW</span>
                    )}
                  </div>
                  <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginTop: 3 }}>
                    {p.company} · {p.location} · {formatYears(p)}
                  </div>
                </div>
                <div style={{ textAlign: 'right', flex: 'none', marginLeft: 10 }}>
                  <div style={{ fontSize: 13.5, fontWeight: 600, color: 'var(--ink)' }}>{formatSalary(p)}</div>
                  <div style={{
                    fontSize: 11, color: 'var(--text-5)', textTransform: 'uppercase',
                    letterSpacing: '0.05em', marginTop: 2,
                  }}>{p.ats}</div>
                </div>
              </div>
            )
          })}
          {!visible.length && (
            <div style={{ color: 'var(--text-4)', fontSize: 13, padding: '30px 4px' }}>
              No postings match these filters{postings.length === 0 && ' — run python -m src.refresh to fill the store'}.
            </div>
          )}
        </div>
      </div>

      {selected.size > 0 && (
        <div style={{
          position: 'absolute', left: '50%', bottom: 26, transform: 'translateX(-50%)',
          display: 'flex', alignItems: 'center', gap: 16, background: '#2B2723',
          color: '#F6F1E8', padding: '12px 14px 12px 20px', borderRadius: 16,
          boxShadow: '0 10px 30px rgba(43,39,35,0.28)', animation: 'fadeUp .25s ease', zIndex: 20,
        }}>
          <span style={{ fontSize: 13.5, fontWeight: 500 }}>{selected.size} roles selected</span>
          <div style={{ width: 1, height: 20, background: '#4B443C' }} />
          <label style={{ display: 'flex', alignItems: 'center', gap: 7, cursor: 'pointer' }}>
            <input type="checkbox" checked={autonomous}
              onChange={(e) => setAutonomous(e.target.checked)}
              style={{ accentColor: '#D9A441', width: 14, height: 14 }} />
            <span style={{ fontSize: 12.5, color: '#E4C98E', fontWeight: 600 }}>Autonomous</span>
          </label>
          <button onClick={openApply} style={{
            background: '#BC5A3C', color: '#FCEFE7', border: 'none', padding: '9px 18px',
            borderRadius: 11, fontSize: 13.5, fontWeight: 600, cursor: 'pointer',
          }}>Apply via Claude Code →</button>
          <button onClick={() => setSelected(new Set())} style={{
            background: 'transparent', color: '#A79E90', border: 'none',
            cursor: 'pointer', fontSize: 18, padding: '0 4px',
          }}>×</button>
        </div>
      )}

      {detail && <JobDetailModal posting={detail} onClose={() => setDetail(null)} />}
    </div>
  )
}
