import { useState } from 'react'
import { formatSalary, formatYears, monogram } from '../api'
import type { Posting } from '../types'

const FILTERS = [
  ['all', 'All roles'],
  ['new', 'New only'],
  ['unapplied', 'Not applied'],
  ['remote', 'Remote'],
] as const

type Filter = (typeof FILTERS)[number][0]

interface Props {
  postings: Posting[]
  note?: string
  selected: Set<string>
  setSelected: (s: Set<string>) => void
  autonomous: boolean
  setAutonomous: (v: boolean) => void
  openApply: () => void
}

export default function PostingsPage({
  postings, note, selected, setSelected, autonomous, setAutonomous, openApply,
}: Props) {
  const [filter, setFilter] = useState<Filter>('all')

  const visible = postings.filter((p) => {
    if (filter === 'new') return p.is_new
    if (filter === 'unapplied') return !p.already_applied
    if (filter === 'remote') return p.remote || /remote/i.test(p.location)
    return true
  })

  const toggle = (url: string) => {
    const next = new Set(selected)
    if (next.has(url)) next.delete(url)
    else next.add(url)
    setSelected(next)
  }

  const unapplied = postings.filter((p) => !p.already_applied).length

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0, position: 'relative' }}>
      <header className="page-header">
        <h1>Postings</h1>
        <p>{unapplied} roles pass your baseline · select to queue an application</p>
        {note && <p style={{ color: 'var(--amber-text)' }}>{note}</p>}
        <div style={{ display: 'flex', gap: 8, marginTop: 14, flexWrap: 'wrap' }}>
          {FILTERS.map(([k, label]) => (
            <button key={k} onClick={() => setFilter(k)} style={{
              fontSize: 12.5, fontWeight: 600, padding: '7px 14px', borderRadius: 20,
              cursor: 'pointer', whiteSpace: 'nowrap',
              border: `1px solid ${filter === k ? 'var(--clay)' : 'var(--border-2)'}`,
              background: filter === k ? 'var(--clay)' : 'var(--chip)',
              color: filter === k ? 'var(--on-clay)' : 'var(--text-2)',
            }}>{label}</button>
          ))}
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
                    {p.is_new && (
                      <span className="tag" style={{ color: 'var(--sage-text)', background: 'var(--sage-soft)' }}>NEW</span>
                    )}
                    {p.already_applied && (
                      <span className="tag" style={{ color: 'var(--text-3)', background: 'var(--chip)' }}>APPLIED</span>
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
              No postings match this filter{postings.length === 0 && ' — run python -m src.refresh to fill the store'}.
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
    </div>
  )
}
