import { useEffect, useState } from 'react'
import { fetchEEO, removeEEO, saveEEO } from '../api'
import type { EEOStatus } from '../types'

/* Voluntary self-identification card. Values are NEVER displayed — the API
   only ever reports Set / Prefer not to say / Not set, and the edit form
   always starts blank. Purple here is informational, never a warning. */

const OPTIONS: Record<string, string[]> = {
  gender: ['Male', 'Female', 'Non-binary', 'I don’t wish to answer'],
  race_ethnicity: [
    'American Indian or Alaska Native', 'Asian', 'Black or African American',
    'Hispanic or Latino', 'Native Hawaiian or Other Pacific Islander', 'White',
    'Two or More Races', 'I don’t wish to answer',
  ],
  hispanic_latino: ['Yes', 'No', 'I don’t wish to answer'],
  veteran_status: [
    'I am not a protected veteran',
    'I identify as one or more of the classifications of a protected veteran',
    'I don’t wish to answer',
  ],
  disability_status: [
    'No, I do not have a disability',
    'Yes, I have a disability, or have had one in the past',
    'I don’t wish to answer',
  ],
}

const STATUS_LABEL = { set: 'Set', prefer_not: 'Prefer not to say', not_set: 'Not set' } as const

function LockGlyph() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="var(--purple)"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flex: 'none' }}>
      <rect x="3" y="11" width="18" height="11" rx="2" />
      <path d="M7 11V7a5 5 0 0 1 10 0v4" />
    </svg>
  )
}

export default function EEOCard() {
  const [status, setStatus] = useState<EEOStatus | null>(null)
  const [editing, setEditing] = useState(false)
  const [values, setValues] = useState<Record<string, string>>({})
  const [confirmRemove, setConfirmRemove] = useState(false)
  const [note, setNote] = useState('')

  useEffect(() => { fetchEEO().then(setStatus).catch(() => {}) }, [])

  if (!status) return null

  const save = async () => {
    const filled = Object.fromEntries(
      Object.entries(values).filter(([, v]) => v.trim()))
    if (!Object.keys(filled).length) { setEditing(false); return }
    try {
      setStatus(await saveEEO(filled))
      setValues({})
      setEditing(false)
      setNote('')
    } catch (e) { setNote(String((e as Error).message)) }
  }

  const removeAll = async () => {
    try {
      setStatus(await removeEEO())
      setConfirmRemove(false)
      setNote('')
    } catch (e) { setNote(String((e as Error).message)) }
  }

  return (
    <div className="card" style={{ padding: '22px 24px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <div className="serif" style={{ fontSize: 16, fontWeight: 600, flex: 1 }}>
          Self-identification
        </div>
        {status.present ? (
          <span className="badge-pill" style={{
            display: 'inline-flex', alignItems: 'center', gap: 6,
            background: 'var(--purple-soft)', color: 'var(--purple-text)',
          }}>
            <LockGlyph />
            On this device only
          </span>
        ) : (
          <span className="badge-pill" style={{ background: 'var(--chip)', color: 'var(--text-3)' }}>
            Optional
          </span>
        )}
      </div>

      {!status.present && !editing && (
        <>
          <p style={{ fontSize: 13, color: 'var(--text-3)', lineHeight: 1.55, margin: '10px 0 12px' }}>
            Many applications ask about gender, race, veteran status and disability.
            Answering is voluntary, and skipping never counts against you.
          </p>
          <div style={{
            display: 'flex', gap: 10, alignItems: 'flex-start',
            background: 'var(--purple-soft)', borderRadius: 11, padding: '12px 14px',
            marginBottom: 14,
          }}>
            <LockGlyph />
            <div style={{ fontSize: 12.5, color: 'var(--purple-text)', lineHeight: 1.55 }}>
              If you add these, they&rsquo;re written to a separate file on this
              device. They are never synced to the cloud, never shown in your
              answer history, and never used for anything but filling these
              specific questions.
            </div>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
            <button className="btn-soft" onClick={() => setEditing(true)}>
              Add self-identification
            </button>
            <span style={{ fontSize: 12, color: 'var(--text-4)' }}>
              Leave it blank and the agent skips these questions.
            </span>
          </div>
        </>
      )}

      {status.present && !editing && (
        <>
          <div style={{
            border: '1px solid var(--border-2)', borderRadius: 11,
            background: 'var(--bg-app)', margin: '14px 0 12px', overflow: 'hidden',
          }}>
            {status.fields.map((f, i) => (
              <div key={f.key} style={{
                display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                padding: '10px 14px',
                borderTop: i > 0 ? '1px solid var(--divider)' : 'none',
              }}>
                <span style={{ fontSize: 13, color: 'var(--text-2)' }}>{f.label}</span>
                <span style={{
                  fontSize: 12.5, fontWeight: 600,
                  color: f.status === 'not_set' ? 'var(--text-5)' : 'var(--text-2)',
                }}>{STATUS_LABEL[f.status]}</span>
              </div>
            ))}
          </div>
          <p style={{ fontSize: 12, color: 'var(--text-4)', lineHeight: 1.5, margin: '0 0 14px' }}>
            Only which questions you&rsquo;ve answered is shown here — the answers
            themselves aren&rsquo;t displayed anywhere in the app.
          </p>
          <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
            <button className="btn-soft" onClick={() => setEditing(true)}>Edit answers</button>
            {confirmRemove ? (
              <span style={{ fontSize: 12.5, display: 'flex', alignItems: 'center', gap: 10 }}>
                <span style={{ color: 'var(--text-3)' }}>
                  Delete the local file? This can&rsquo;t be undone.
                </span>
                <button onClick={removeAll} style={{
                  background: 'none', border: 'none', padding: 0, cursor: 'pointer',
                  fontSize: 12.5, fontWeight: 700, color: 'var(--clay-text)',
                }}>Yes, remove all</button>
                <button onClick={() => setConfirmRemove(false)} style={{
                  background: 'none', border: 'none', padding: 0, cursor: 'pointer',
                  fontSize: 12.5, fontWeight: 600, color: 'var(--text-4)',
                }}>Cancel</button>
              </span>
            ) : (
              <button onClick={() => setConfirmRemove(true)} style={{
                background: 'none', border: 'none', padding: 0, cursor: 'pointer',
                fontSize: 13, fontWeight: 600, color: 'var(--clay-text)',
              }}>Remove all</button>
            )}
          </div>
        </>
      )}

      {editing && (
        <>
          <p style={{ fontSize: 12.5, color: 'var(--text-4)', lineHeight: 1.5, margin: '10px 0 14px' }}>
            Current answers aren&rsquo;t shown. Anything you pick here overwrites
            that question; anything you leave on &ldquo;Keep as is&rdquo; stays untouched.
          </p>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '14px 20px', marginBottom: 16 }}>
            {status.fields.map((f) => (
              <div key={f.key}>
                <label style={{
                  fontSize: 11, letterSpacing: '.05em', textTransform: 'uppercase',
                  color: 'var(--text-4)', marginBottom: 6, display: 'block',
                }}>{f.label}</label>
                <select
                  value={values[f.key] ?? ''}
                  onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
                  style={{
                    width: '100%', border: '1px solid var(--border-3)', background: 'var(--bg-card)',
                    borderRadius: 10, padding: '10px 12px', fontSize: 13, outline: 'none',
                    color: 'var(--ink)', cursor: 'pointer',
                  }}
                >
                  <option value="">Keep as is</option>
                  {OPTIONS[f.key]?.map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              </div>
            ))}
          </div>
          <div style={{ display: 'flex', gap: 10 }}>
            <button className="btn-primary" onClick={save}>Save to this device</button>
            <button className="btn-soft" onClick={() => { setEditing(false); setValues({}) }}>
              Cancel
            </button>
          </div>
        </>
      )}

      {note && <div style={{ fontSize: 12.5, color: 'var(--clay-text)', marginTop: 12 }}>{note}</div>}
    </div>
  )
}
