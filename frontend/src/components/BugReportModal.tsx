import { useState } from 'react'
import { reportBug } from '../api'

/* Report-a-bug modal on the shared modal shell (see ProfileModals). Reports
   land in data/bug-reports.jsonl for the daily dev-loop agent to triage —
   nothing leaves this machine. */

const SCRIM: React.CSSProperties = {
  position: 'fixed', inset: 0, background: 'rgba(43,39,35,0.42)', display: 'flex',
  alignItems: 'center', justifyContent: 'center', zIndex: 70, animation: 'fadeUp .2s ease',
}

const SHELL: React.CSSProperties = {
  width: 420, maxWidth: '92vw', background: 'var(--bg-app)', borderRadius: 16,
  padding: 22, boxShadow: '0 24px 60px rgba(43,39,35,0.35)',
}

export default function BugReportModal({ page, onCancel, onSent }: {
  page: string
  onCancel: () => void
  onSent: () => void
}) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const submit = async () => {
    if (!text.trim() || busy) return
    setBusy(true)
    setError('')
    try {
      await reportBug(text.trim(), page)
      onSent()
    } catch (e) {
      setError(String((e as Error).message))
      setBusy(false)
    }
  }

  return (
    <div style={SCRIM} onClick={onCancel}>
      <div style={SHELL} onClick={(e) => e.stopPropagation()}>
        <div className="serif" style={{ fontSize: 19, fontWeight: 600 }}>Report a bug</div>
        <p style={{ fontSize: 13, color: 'var(--text-3)', lineHeight: 1.55, margin: '8px 0 14px' }}>
          What went wrong, and what were you doing? Reports stay on this machine
          and feed the nightly improvement loop.
        </p>
        <textarea
          autoFocus
          value={text}
          rows={5}
          placeholder="e.g. The postings filter reset itself after a refresh…"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submit()
          }}
          style={{
            width: '100%', border: '1px solid var(--border-3)', background: 'var(--bg-card)',
            borderRadius: 10, padding: '11px 13px', fontSize: 13.5, outline: 'none',
            color: 'var(--ink)', marginBottom: 14, resize: 'vertical',
            fontFamily: 'inherit', lineHeight: 1.5,
          }}
        />
        {error && (
          <div style={{ fontSize: 12.5, color: 'var(--clay-text)', marginBottom: 12 }}>{error}</div>
        )}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
          <button className="btn-soft" onClick={onCancel}>Cancel</button>
          <button className="btn-primary" disabled={!text.trim() || busy} onClick={submit}>
            {busy ? 'Sending…' : 'Send report'}
          </button>
        </div>
      </div>
    </div>
  )
}
