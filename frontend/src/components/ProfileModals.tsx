import { useState } from 'react'
import type { ProfileSummary } from '../types'

/* Switch-profile confirm + new-profile name prompt — both on the apply-modal
   shell (scrim rgba(43,39,35,.42), r16, 22px padding, ~380px). */

const SCRIM: React.CSSProperties = {
  position: 'fixed', inset: 0, background: 'rgba(43,39,35,0.42)', display: 'flex',
  alignItems: 'center', justifyContent: 'center', zIndex: 70, animation: 'fadeUp .2s ease',
}

const SHELL: React.CSSProperties = {
  width: 380, maxWidth: '92vw', background: 'var(--bg-app)', borderRadius: 16,
  padding: 22, boxShadow: '0 24px 60px rgba(43,39,35,0.35)',
}

export function SwitchConfirmModal({ target, onCancel, onConfirm }: {
  target: ProfileSummary
  onCancel: () => void
  onConfirm: () => void
}) {
  const [busy, setBusy] = useState(false)
  return (
    <div style={SCRIM} onClick={onCancel}>
      <div style={SHELL} onClick={(e) => e.stopPropagation()}>
        <div className="serif" style={{ fontSize: 19, fontWeight: 600 }}>
          Switch to {target.name}?
        </div>
        <p style={{ fontSize: 13, color: 'var(--text-3)', lineHeight: 1.55, margin: '8px 0 14px' }}>
          Every page re-scopes to that profile — postings, applications, résumé
          and answer history. The current profile&rsquo;s data stays untouched on
          this machine.
        </p>
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10, background: 'var(--bg-card)',
          border: '1px solid var(--border-2)', borderRadius: 11, padding: '10px 12px',
          marginBottom: 18,
        }}>
          <div style={{
            width: 30, height: 30, borderRadius: '50%', background: 'var(--accent-soft)',
            color: 'var(--clay-text)', display: 'flex', alignItems: 'center',
            justifyContent: 'center', fontWeight: 600, fontSize: 12, flex: 'none',
          }}>
            {target.name.split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase()}
          </div>
          <div style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.4 }}>
            Setup is {target.completeness}% complete
            {target.completeness < 60 && ' — the wizard opens on switch'}.
          </div>
        </div>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
          <button className="btn-soft" onClick={onCancel}>Cancel</button>
          <button
            className="btn-primary" disabled={busy}
            onClick={() => { setBusy(true); onConfirm() }}
          >{busy ? 'Switching…' : 'Switch profile'}</button>
        </div>
      </div>
    </div>
  )
}

export function NewProfileModal({ onCancel, onCreate }: {
  onCancel: () => void
  onCreate: (name: string) => Promise<void>
}) {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const submit = async () => {
    if (!name.trim() || busy) return
    setBusy(true)
    setError('')
    try {
      await onCreate(name.trim())
    } catch (e) {
      setError(String((e as Error).message))
      setBusy(false)
    }
  }

  return (
    <div style={SCRIM} onClick={onCancel}>
      <div style={SHELL} onClick={(e) => e.stopPropagation()}>
        <div className="serif" style={{ fontSize: 19, fontWeight: 600 }}>New profile</div>
        <p style={{ fontSize: 13, color: 'var(--text-3)', lineHeight: 1.55, margin: '8px 0 14px' }}>
          A profile keeps one person&rsquo;s facts, résumé, history and criteria
          together under <span style={{ fontFamily: 'ui-monospace,Menlo,monospace', fontSize: 12 }}>
          profiles/</span> on this machine. Setup opens right after.
        </p>
        <input
          autoFocus
          value={name}
          placeholder="Full name — e.g. Priya Raman"
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
          style={{
            width: '100%', border: '1px solid var(--border-3)', background: 'var(--bg-card)',
            borderRadius: 10, padding: '11px 13px', fontSize: 14, outline: 'none',
            color: 'var(--ink)', marginBottom: 14,
          }}
        />
        {error && (
          <div style={{ fontSize: 12.5, color: 'var(--clay-text)', marginBottom: 12 }}>{error}</div>
        )}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
          <button className="btn-soft" onClick={onCancel}>Cancel</button>
          <button className="btn-primary" disabled={!name.trim() || busy} onClick={submit}>
            {busy ? 'Creating…' : 'Create profile'}
          </button>
        </div>
      </div>
    </div>
  )
}
