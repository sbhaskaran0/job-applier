import { monogram } from '../api'
import type { ApplicationRecord } from '../types'

const STATUS_META: Record<string, { label: string; bg: string; fg: string; dot: string }> = {
  submitted: { label: 'Submitted', bg: 'var(--sage-soft)', fg: 'var(--sage-text)', dot: 'var(--sage)' },
  manual_submission: { label: 'Manual submit', bg: 'var(--amber-soft)', fg: 'var(--amber-text)', dot: 'var(--amber)' },
  attempted: { label: 'Attempted', bg: 'var(--purple-soft)', fg: 'var(--purple-text)', dot: 'var(--purple)' },
  parked: { label: 'Parked', bg: 'var(--accent-soft)', fg: 'var(--clay-text)', dot: 'var(--clay)' },
}

const GRID = { display: 'grid', gridTemplateColumns: '1.6fr 1fr 0.7fr 0.9fr', gap: 14 } as const

export default function ApplicationsPage({ applications }: { applications: ApplicationRecord[] }) {
  const meta = (s: string) => STATUS_META[s] ?? STATUS_META.attempted
  const count = (s: string) => applications.filter((a) => a.status === s).length
  const stats = [
    { value: applications.length, label: 'Total tracked', dot: 'var(--dot-faint)' },
    { value: count('submitted'), label: 'Submitted', dot: 'var(--sage)' },
    { value: count('manual_submission'), label: 'Manual submit', dot: 'var(--amber)' },
    { value: count('attempted'), label: 'Needs you', dot: 'var(--purple)' },
  ]
  const sorted = [...applications].sort((a, b) => (b.date || '').localeCompare(a.date || ''))

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <header className="page-header" style={{ paddingBottom: 18 }}>
        <h1>Applications</h1>
        <p>Every submit the agent has tracked, verified from the confirmation page.</p>
        <div style={{ display: 'flex', gap: 14, marginTop: 16 }}>
          {stats.map((s) => (
            <div key={s.label} className="card" style={{ borderRadius: 12, padding: '13px 18px', minWidth: 120 }}>
              <div className="serif" style={{ fontSize: 26, fontWeight: 600, lineHeight: 1 }}>{s.value}</div>
              <div style={{
                fontSize: 12, color: 'var(--text-3)', marginTop: 5,
                display: 'flex', alignItems: 'center', gap: 6,
              }}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: s.dot, flex: 'none' }} />
                {s.label}
              </div>
            </div>
          ))}
        </div>
      </header>
      <div style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', padding: '8px 34px 40px' }}>
        <div style={{ maxWidth: 940 }}>
          <div style={{
            ...GRID, padding: '14px 16px 8px', fontSize: 11, fontWeight: 600,
            color: 'var(--text-5)', letterSpacing: '0.06em', textTransform: 'uppercase',
          }}>
            <span>Role</span><span>Company</span><span>Date</span><span>Status</span>
          </div>
          {sorted.map((a, i) => {
            const m = meta(a.status)
            const d = a.date ? new Date(`${a.date}T00:00`) : null
            return (
              <div key={`${a.company}-${a.job_title}-${i}`} style={{
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
