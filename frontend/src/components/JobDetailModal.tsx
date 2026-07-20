import { useEffect, useState } from 'react'
import { fetchPostingDetail, formatSalary, formatYears, monogram } from '../api'
import type { Posting } from '../types'

interface Props {
  posting: Posting
  onClose: () => void
}

export default function JobDetailModal({ posting, onClose }: Props) {
  const [description, setDescription] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let stale = false
    fetchPostingDetail(posting.url)
      .then((d) => {
        if (stale) return
        if (d.found && d.description) setDescription(d.description)
        else setError(d.note ?? 'No description available for this posting.')
      })
      .catch(() => { if (!stale) setError('Could not load the job description.') })
    return () => { stale = true }
  }, [posting.url])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div onClick={onClose} style={{
      position: 'fixed', inset: 0, background: 'rgba(43,39,35,0.42)', display: 'flex',
      alignItems: 'center', justifyContent: 'center', zIndex: 60, animation: 'fadeUp .2s ease',
    }}>
      <div onClick={(e) => e.stopPropagation()} style={{
        width: 680, maxWidth: '92vw', maxHeight: '84vh', display: 'flex',
        flexDirection: 'column', background: 'var(--bg-app)', borderRadius: 20,
        overflow: 'hidden', boxShadow: '0 24px 60px rgba(43,39,35,0.35)',
      }}>
        <div style={{
          padding: '22px 24px 18px', borderBottom: '1px solid var(--border-1)',
          background: 'var(--bg-rail)', display: 'flex', alignItems: 'center', gap: 13,
        }}>
          <div className="mono-tile" style={{ width: 40, height: 40, borderRadius: 11, fontSize: 15, flex: 'none' }}>
            {monogram(posting.company)}
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="serif" style={{ fontSize: 19, fontWeight: 600, lineHeight: 1.15 }}>
              {posting.title}
            </div>
            <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginTop: 3 }}>
              {posting.company} · {posting.location} · {formatYears(posting)} · {formatSalary(posting)}
            </div>
          </div>
          <button onClick={onClose} aria-label="Close" style={{
            background: 'transparent', color: 'var(--text-4)', border: 'none',
            cursor: 'pointer', fontSize: 20, padding: '0 2px', flex: 'none', lineHeight: 1,
          }}>×</button>
        </div>

        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '18px 24px' }}>
          {description ? (
            <div style={{
              fontSize: 13.5, lineHeight: 1.65, color: 'var(--text-2)',
              whiteSpace: 'pre-wrap', overflowWrap: 'break-word',
            }}>{description}</div>
          ) : (
            <div style={{ fontSize: 13, color: 'var(--text-4)', padding: '18px 2px' }}>
              {error ?? 'Loading job description…'}
            </div>
          )}
        </div>

        <div style={{
          display: 'flex', gap: 11, padding: '14px 24px 20px',
          borderTop: '1px solid var(--border-1)',
        }}>
          <button onClick={onClose} style={{
            flex: 'none', background: 'transparent', color: 'var(--text-3)',
            border: '1px solid var(--border-2)', padding: '10px 20px', borderRadius: 11,
            fontSize: 13.5, fontWeight: 600, cursor: 'pointer',
          }}>Close</button>
          <a href={posting.url} target="_blank" rel="noreferrer" style={{
            flex: 1, border: 'none', padding: '10px 20px', borderRadius: 11,
            fontSize: 13.5, fontWeight: 600, cursor: 'pointer', textAlign: 'center',
            textDecoration: 'none', color: 'var(--on-clay)', background: 'var(--clay)',
          }}>Open posting ↗</a>
        </div>
      </div>
    </div>
  )
}
