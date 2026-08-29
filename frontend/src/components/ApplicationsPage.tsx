import { useEffect, useState } from 'react'
import { fetchApplications, monogram, setApplicationOutcome } from '../api'
import type { ApplicationRecord, ApplicationStats } from '../types'

const STATUS_META: Record<string, { label: string; bg: string; fg: string; dot: string }> = {
  submitted: { label: 'Submitted', bg: 'var(--sage-soft)', fg: 'var(--sage-text)', dot: 'var(--sage)' },
  manual_submission: { label: 'Manual submit', bg: 'var(--amber-soft)', fg: 'var(--amber-text)', dot: 'var(--amber)' },
  attempted: { label: 'Attempted', bg: 'var(--purple-soft)', fg: 'var(--purple-text)', dot: 'var(--purple)' },
  parked: { label: 'Parked', bg: 'var(--accent-soft)', fg: 'var(--clay-text)', dot: 'var(--clay)' },
}

// The reply vocabulary (JOB-107) — orthogonal to STATUS_META, which only says
// whether we finished the form. Order matches src/data.APPLICATION_OUTCOMES.
const OUTCOME_OPTIONS: { value: string; label: string }[] = [
  { value: 'none', label: 'No reply yet' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'screen', label: 'Screen' },
  { value: 'interview', label: 'Interview' },
  { value: 'offer', label: 'Offer' },
  { value: 'ghosted', label: 'Ghosted' },
]

const GRID = { display: 'grid', gridTemplateColumns: '1.5fr 0.9fr 0.55fr 0.85fr 0.95fr', gap: 14 } as const

export default function ApplicationsPage({ applications }: { applications: ApplicationRecord[] }) {
  // Local copy so an outcome change can update optimistically; the prop stays
  // the source of truth and re-seeds it whenever App re-fetches.
  const [rows, setRows] = useState<ApplicationRecord[]>(applications)
  const [stats, setStats] = useState<ApplicationStats | null>(null)
  const [error, setError] = useState('')
  const [pending, setPending] = useState('')

  useEffect(() => setRows(applications), [applications])

  // App.tsx keeps only the array from this endpoint, so the page reads it once
  // itself to pick up `stats`. Deliberate second read of a cheap file-backed
  // route — the response-rate denominator is defined server-side and must not
  // be recomputed here, or the two definitions would drift.
  useEffect(() => {
    fetchApplications().then((r) => setStats(r.stats ?? null)).catch(() => setStats(null))
  }, [])

  const meta = (s: string) => STATUS_META[s] ?? STATUS_META.attempted
  const count = (s: string) => rows.filter((a) => a.status === s).length
  const tiles: { value: string | number; label: string; dot: string; sub?: string }[] = [
    { value: rows.length, label: 'Total tracked', dot: 'var(--dot-faint)' },
    { value: count('submitted'), label: 'Submitted', dot: 'var(--sage)' },
    { value: count('manual_submission'), label: 'Manual submit', dot: 'var(--amber)' },
    { value: count('attempted'), label: 'Needs you', dot: 'var(--purple)' },
    {
      // Raw counts ride along with the percentage so a tiny denominator is
      // visible rather than hidden behind a confident-looking number.
      value: stats ? `${Math.round(stats.response_rate * 100)}%` : '—',
      label: 'Response rate',
      dot: 'var(--clay)',
      sub: stats ? `${stats.responded} / ${stats.submitted} responded` : 'stats unavailable',
    },
  ]
  const sorted = [...rows].sort((a, b) => (b.date || '').localeCompare(a.date || ''))

  async function changeOutcome(row: ApplicationRecord, outcome: string) {
    const key = row.key
    if (!key) return
    const previous = rows
    setError('')
    setPending(key)
    setRows((rs) => rs.map((r) => (r.key === key ? { ...r, outcome } : r)))
    try {
      const res = await setApplicationOutcome(key, outcome)
      setRows((rs) => rs.map((r) => (r.key === key ? res.application : r)))
      setStats(res.stats)
    } catch (e) {
      setRows(previous)
      setError(`Could not save the outcome for ${row.job_title || row.company}: ${(e as Error).message}`)
    } finally {
      setPending('')
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <header className="page-header" style={{ paddingBottom: 18 }}>
        <h1>Applications</h1>
        <p>Every submit the agent has tracked, verified from the confirmation page.</p>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 14, marginTop: 16 }}>
          {tiles.map((s) => (
            <div key={s.label} className="card" style={{ borderRadius: 12, padding: '13px 18px', minWidth: 120 }}>
              <div className="serif" style={{ fontSize: 26, fontWeight: 600, lineHeight: 1 }}>{s.value}</div>
              <div style={{
                fontSize: 12, color: 'var(--text-3)', marginTop: 5,
                display: 'flex', alignItems: 'center', gap: 6,
              }}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: s.dot, flex: 'none' }} />
                {s.label}
              </div>
              {s.sub && (
                <div style={{ fontSize: 11.5, color: 'var(--text-5)', marginTop: 3, paddingLeft: 14 }}>
                  {s.sub}
                </div>
              )}
            </div>
          ))}
        </div>
        {error && (
          <div style={{ fontSize: 12, color: 'var(--clay-text)', marginTop: 12 }}>{error}</div>
        )}
      </header>
      <div style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', padding: '8px 34px 40px' }}>
        <div style={{ maxWidth: 940 }}>
          <div style={{
            ...GRID, padding: '14px 16px 8px', fontSize: 11, fontWeight: 600,
            color: 'var(--text-5)', letterSpacing: '0.06em', textTransform: 'uppercase',
          }}>
            <span>Role</span><span>Company</span><span>Date</span><span>Status</span><span>Outcome</span>
          </div>
          {sorted.map((a, i) => {
            const m = meta(a.status)
            const d = a.date ? new Date(`${a.date}T00:00`) : null
            return (
              <div key={a.key || `${a.company}-${a.job_title}-${i}`} style={{
                ...GRID, alignItems: 'center', padding: '14px 16px',
                borderTop: '1px solid var(--divider-2)',
              }}>
                <div style={{ minWidth: 0 }}>
                  <div style={{
                    fontSize: 13.5, fontWeight: 600, color: 'var(--ink)',
                    whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                  }} title={a.job_title}>
                    <a href={a.url} target="_blank" rel="noreferrer" style={{ color: 'inherit' }}>
                      {a.job_title}
                    </a>
                  </div>
                  <div style={{ fontSize: 11.5, color: 'var(--text-5)', marginTop: 2 }}>
                    {a.fields?.length ? `${a.fields.length} fields` : '—'}
                  </div>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 9, minWidth: 0 }}>
                  <div className="mono-tile" style={{ width: 26, height: 26, borderRadius: 7, fontSize: 11 }}>
                    {monogram(a.company)}
                  </div>
                  <span style={{
                    fontSize: 13, color: 'var(--text-2)', whiteSpace: 'nowrap',
                    overflow: 'hidden', textOverflow: 'ellipsis',
                  }}>{a.company}</span>
                </div>
                <span style={{ fontSize: 12.5, color: 'var(--text-3)' }}>
                  {d ? d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : '—'}
                </span>
                <span className="badge-pill" style={{ background: m.bg, color: m.fg }}>{m.label}</span>
                <div style={{ minWidth: 0 }}>
                  <select
                    value={a.outcome || 'none'}
                    // The API stamps a key on every row; a row without one means
                    // there is no safe POST target, so the control stays inert.
                    disabled={!a.key || pending === a.key}
                    onChange={(e) => changeOutcome(a, e.target.value)}
                    title={a.outcome_date ? `Recorded ${a.outcome_date}` : 'No outcome recorded'}
                    style={{
                      width: '100%', fontSize: 11.5, fontWeight: 600, padding: '4px 8px',
                      borderRadius: 20, cursor: a.key ? 'pointer' : 'default',
                      color: a.outcome && a.outcome !== 'none' ? 'var(--clay-text)' : 'var(--text-4)',
                      background: a.outcome && a.outcome !== 'none' ? 'var(--accent-soft)' : 'var(--chip)',
                      border: '1px solid var(--border-2)',
                      opacity: pending === a.key ? 0.55 : 1,
                    }}
                  >
                    {OUTCOME_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </select>
                </div>
              </div>
            )
          })}
          {!sorted.length && (
            <div style={{ color: 'var(--text-4)', fontSize: 13, padding: '30px 16px' }}>
              No applications tracked yet.
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
