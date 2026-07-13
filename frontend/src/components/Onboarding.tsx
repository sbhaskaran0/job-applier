import { useRef, useState } from 'react'
import { monogram, updateProfile, uploadFile } from '../api'
import type { Connection, Profile } from '../types'

const STEP_DEFS = [
  { name: 'Welcome', title: 'Welcome to Applyer', body: 'A quick setup so the agent applies as you would — never inventing anything, never submitting without your say-so (unless you turn on autonomous mode).' },
  { name: 'Your facts', title: 'The facts that fill forms', body: 'These get matched to form fields and filled verbatim. Leave anything blank to have the agent fall back to your history or knowledge base instead.' },
  { name: 'Résumé', title: 'Add your résumé', body: "Uploaded to every form. Add a .docx version and the agent can tailor a bespoke résumé per role — reordering and re-emphasizing what's already true." },
  { name: 'Knowledge base', title: 'Teach it your voice', body: 'Past cover letters, stories, and writing samples. The agent pulls from these to craft open-ended answers that sound like you — not a generic AI.' },
  { name: 'Connect tools', title: 'Connect your tools', body: 'Authorize what the agent needs. Claude Code and the MCP server are required; Gmail lets it clear email verification codes on its own.' },
]

const WELCOME_ITEMS = [
  { num: '1', title: 'Search a curated watchlist', desc: 'Hand-picked companies, ranked semantically against your query.' },
  { num: '2', title: 'Fill any ATS, intelligently', desc: 'Greenhouse, Lever, Ashby, Workday — profile → history → your voice.' },
  { num: '3', title: 'You stay in control', desc: 'It pauses when unsure and never submits without your explicit go-ahead.' },
]

const FORM_FIELDS: { key: string; label: string; full?: boolean }[] = [
  { key: 'first_name', label: 'First name' },
  { key: 'last_name', label: 'Last name' },
  { key: 'email', label: 'Email' },
  { key: 'phone', label: 'Phone' },
  { key: 'location', label: 'Location' },
  { key: 'current_title', label: 'Current title' },
  { key: 'desired_salary', label: 'Desired salary' },
  { key: 'notice_period', label: 'Notice period' },
  { key: 'linkedin_url', label: 'LinkedIn URL', full: true },
  { key: 'work_authorization', label: 'Work authorization', full: true },
]

interface Props {
  profile: Profile
  connections: Connection[]
  onClose: () => void
  onProfileSaved: (p: Profile) => void
}

export default function Onboarding({ profile, connections, onClose, onProfileSaved }: Props) {
  const [step, setStep] = useState(0)
  const [facts, setFacts] = useState<Record<string, string>>({ ...profile.facts })
  const [saveNote, setSaveNote] = useState('')
  const [contextFiles, setContextFiles] = useState(profile.context_files)
  const [resumeNote, setResumeNote] = useState('')
  const resumeInput = useRef<HTMLInputElement>(null)
  const contextInput = useRef<HTMLInputElement>(null)
  const cur = STEP_DEFS[step]

  const saveFactsIfDirty = async () => {
    const dirty = Object.fromEntries(
      Object.entries(facts).filter(([k, v]) => (profile.facts[k] ?? '') !== v),
    )
    if (!Object.keys(dirty).length) return
    try {
      const updated = await updateProfile(dirty)
      onProfileSaved(updated)
      setSaveNote('Saved to user_profile.yaml ✓')
    } catch (e) {
      setSaveNote(String((e as Error).message))
      throw e
    }
  }

  const next = async () => {
    if (step === 1) {
      try { await saveFactsIfDirty() } catch { return }
    }
    if (step >= 4) { onClose(); return }
    setStep(step + 1)
  }

  const onResume = async (file: File | undefined) => {
    if (!file) return
    try {
      const r = await uploadFile('resume', file)
      setResumeNote(`✓ ${r.saved} uploaded${r.text_synced ? ' · text synced' : ''}`)
    } catch (e) { setResumeNote(String((e as Error).message)) }
  }

  const onContext = async (files: FileList | null) => {
    for (const f of Array.from(files ?? [])) {
      try {
        const r = await uploadFile('context', f)
        setContextFiles(r.context_files)
      } catch { /* per-file failure — the list simply doesn't grow */ }
    }
  }

  const inputStyle = {
    width: '100%', border: '1px solid var(--border-3)', background: 'var(--bg-card)',
    borderRadius: 10, padding: '11px 13px', fontSize: 14, outline: 'none',
    color: 'var(--ink)',
  } as const

  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'var(--overlay-base)', zIndex: 80,
      display: 'flex', flexDirection: 'column', animation: 'fadeUp .25s ease',
    }}>
      <div style={{
        padding: '22px 34px', display: 'flex', alignItems: 'center',
        justifyContent: 'space-between', borderBottom: '1px solid var(--border-3)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 11 }}>
          <div style={{
            width: 30, height: 30, borderRadius: 9, background: 'var(--clay)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', flex: 'none',
          }}>
            <div style={{
              width: 11, height: 11, border: '2.3px solid var(--on-clay)', borderRadius: '50%',
              borderTopColor: 'transparent', transform: 'rotate(-45deg)',
            }} />
          </div>
          <span className="serif" style={{ fontSize: 17, fontWeight: 600 }}>Set up Applyer</span>
        </div>
        <button onClick={onClose} style={{
          background: 'transparent', border: 'none', color: 'var(--text-4)',
          fontSize: 22, cursor: 'pointer',
        }}>×</button>
      </div>

      <div style={{ padding: '22px 34px 0', maxWidth: 720, margin: '0 auto', width: '100%' }}>
        <div style={{ display: 'flex', gap: 8 }}>
          {STEP_DEFS.map((s, i) => (
            <div key={s.name} style={{
              flex: 1, height: 5, borderRadius: 3,
              background: i <= step ? 'var(--clay)' : 'var(--bar-track)',
            }} />
          ))}
        </div>
        <div style={{ fontSize: 12, color: 'var(--text-4)', marginTop: 10, letterSpacing: '0.03em' }}>
          Step {step + 1} of 5 · {cur.name}
        </div>
      </div>

      <div style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', padding: '26px 34px' }}>
        <div style={{ maxWidth: 720, margin: '0 auto' }}>
          <h2 className="serif" style={{ fontSize: 29, fontWeight: 600, margin: '0 0 8px', letterSpacing: '-0.02em' }}>
            {cur.title}
          </h2>
          <p style={{ fontSize: 14, color: 'var(--text-3)', margin: '0 0 26px', lineHeight: 1.55, maxWidth: 560 }}>
            {cur.body}
          </p>

          {step === 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {WELCOME_ITEMS.map((w) => (
                <div key={w.num} style={{
                  display: 'flex', gap: 14, background: 'var(--bg-app)',
                  border: '1px solid var(--border-2)', borderRadius: 14, padding: '16px 18px',
                }}>
                  <div style={{
                    width: 34, height: 34, borderRadius: 9, background: 'var(--accent-soft)',
                    color: 'var(--clay-text)', display: 'flex', alignItems: 'center',
                    justifyContent: 'center', fontFamily: "'Newsreader',serif",
                    fontWeight: 600, flex: 'none',
                  }}>{w.num}</div>
                  <div>
                    <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 2 }}>{w.title}</div>
                    <div style={{ fontSize: 12.5, color: 'var(--text-3)', lineHeight: 1.45 }}>{w.desc}</div>
                  </div>
                </div>
              ))}
            </div>
          )}

          {step === 1 && (
            <>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px 20px' }}>
                {FORM_FIELDS.map((f) => (
                  <div key={f.key} style={f.full ? { gridColumn: '1 / -1' } : undefined}>
                    <label style={{
                      fontSize: 12, color: 'var(--text-3)', fontWeight: 500,
                      display: 'block', marginBottom: 6,
                    }}>{f.label}</label>
                    <input
                      value={facts[f.key] ?? ''}
                      onChange={(e) => setFacts({ ...facts, [f.key]: e.target.value })}
                      style={inputStyle}
                    />
                  </div>
                ))}
              </div>
              {saveNote && (
                <div style={{ fontSize: 12, color: 'var(--sage-text)', marginTop: 14 }}>{saveNote}</div>
              )}
            </>
          )}

          {step === 2 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              <div
                onClick={() => resumeInput.current?.click()}
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => { e.preventDefault(); onResume(e.dataTransfer.files?.[0]) }}
                style={{
                  border: '2px dashed var(--border-4)', borderRadius: 16, padding: 34,
                  textAlign: 'center', background: 'var(--bg-app)', cursor: 'pointer',
                }}
              >
                <div style={{
                  width: 48, height: 48, borderRadius: 12, background: 'var(--accent-soft)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', margin: '0 auto 12px',
                }}>
                  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--clay-text)"
                    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12" />
                  </svg>
                </div>
                <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--ink)' }}>Drop your résumé here</div>
                <div style={{ fontSize: 12.5, color: 'var(--text-4)', marginTop: 4 }}>
                  PDF preferred · add a .docx to unlock per-role tailoring
                </div>
                <input ref={resumeInput} type="file" accept=".pdf,.docx,.txt" hidden
                  onChange={(e) => onResume(e.target.files?.[0])} />
              </div>
              {(profile.resume_pdf || resumeNote) && (
                <div style={{
                  display: 'flex', alignItems: 'center', gap: 12, padding: '14px 16px',
                  background: 'var(--bg-card)', border: '1px solid var(--border-1)', borderRadius: 12,
                }}>
                  <div style={{
                    width: 34, height: 40, borderRadius: 5, background: 'var(--clay)',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    color: 'var(--on-clay)', fontSize: 9, fontWeight: 700, flex: 'none',
                  }}>PDF</div>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 13, fontWeight: 600 }}>resume.pdf</div>
                    <div style={{ fontSize: 11.5, color: 'var(--sage-text)' }}>
                      {resumeNote || '✓ Uploaded · text synced'}
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}

          {step === 3 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              <div
                onClick={() => contextInput.current?.click()}
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => { e.preventDefault(); onContext(e.dataTransfer.files) }}
                style={{
                  border: '2px dashed var(--border-4)', borderRadius: 16, padding: 26,
                  textAlign: 'center', background: 'var(--bg-app)', cursor: 'pointer',
                }}
              >
                <div style={{ fontSize: 13.5, fontWeight: 600, color: 'var(--ink)' }}>
                  Drop cover letters, essays & writing samples
                </div>
                <div style={{ fontSize: 12.5, color: 'var(--text-4)', marginTop: 4 }}>
                  .md · .txt · .pdf — indexed live, never uploaded to forms
                </div>
                <input ref={contextInput} type="file" accept=".md,.txt,.pdf" multiple hidden
                  onChange={(e) => onContext(e.target.files)} />
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                {contextFiles.map((c) => (
                  <div key={c.name} style={{
                    display: 'flex', alignItems: 'center', gap: 10, padding: '12px 14px',
                    background: 'var(--bg-card)', border: '1px solid var(--border-1)', borderRadius: 11,
                  }}>
                    <div style={{
                      width: 28, height: 34, borderRadius: 5, background: 'var(--accent-soft)',
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      color: 'var(--clay-text)', fontSize: 8, fontWeight: 700, flex: 'none',
                    }}>DOC</div>
                    <div style={{ minWidth: 0 }}>
                      <div style={{
                        fontSize: 12.5, fontWeight: 600, whiteSpace: 'nowrap',
                        overflow: 'hidden', textOverflow: 'ellipsis',
                      }}>{c.name}</div>
                      <div style={{ fontSize: 11, color: 'var(--text-4)' }}>{c.kind}</div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {step === 4 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {connections.map((c) => (
                <div key={c.id} style={{
                  display: 'flex', alignItems: 'center', gap: 14, background: 'var(--bg-app)',
                  border: '1px solid var(--border-2)', borderRadius: 14, padding: '15px 18px',
                }}>
                  <div style={{
                    width: 38, height: 38, borderRadius: 10, background: 'var(--accent-soft)',
                    color: 'var(--clay-text)', display: 'flex', alignItems: 'center',
                    justifyContent: 'center', fontFamily: "'Newsreader',serif",
                    fontWeight: 600, flex: 'none',
                  }}>{c.mono || monogram(c.name)}</div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 14, fontWeight: 600 }}>{c.name}</div>
                    <div style={{ fontSize: 12, color: 'var(--text-3)' }}>{c.short}</div>
                  </div>
                  {c.connected ? (
                    <span style={{
                      display: 'flex', alignItems: 'center', gap: 6, fontSize: 12.5,
                      fontWeight: 600, color: 'var(--sage-text)',
                    }}>
                      <span style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--sage)' }} />
                      Connected
                    </span>
                  ) : (
                    <span style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--text-4)' }}>
                      Not detected
                    </span>
                  )}
                </div>
              ))}
              <div style={{ fontSize: 12, color: 'var(--text-4)', lineHeight: 1.5 }}>
                Authorize connectors in Claude Code (<b>/mcp</b>) or claude.ai connector settings —
                this list reflects detected status.
              </div>
            </div>
          )}
        </div>
      </div>

      <div style={{ padding: '18px 34px', borderTop: '1px solid var(--border-3)' }}>
        <div style={{
          maxWidth: 720, margin: '0 auto', display: 'flex',
          alignItems: 'center', justifyContent: 'space-between',
        }}>
          <button
            onClick={() => setStep(Math.max(0, step - 1))}
            style={step === 0 ? { visibility: 'hidden', background: 'transparent', border: 'none' } : {
              background: 'transparent', color: 'var(--text-3)', border: '1px solid var(--border-2)',
              padding: '12px 20px', borderRadius: 12, fontSize: 14, fontWeight: 600, cursor: 'pointer',
            }}
          >← Back</button>
          <button onClick={next} style={{
            background: 'var(--clay)', color: 'var(--on-clay)', border: 'none',
            padding: '12px 26px', borderRadius: 12, fontSize: 14, fontWeight: 600, cursor: 'pointer',
          }}>{step === 4 ? 'Finish setup →' : step === 0 ? "Let's go →" : 'Continue →'}</button>
        </div>
      </div>
    </div>
  )
}
