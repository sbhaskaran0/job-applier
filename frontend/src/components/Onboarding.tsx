import { useEffect, useRef, useState } from 'react'
import {
  deleteContext, fetchCriteria, monogram, pasteContext, updateCriteria,
  updateProfile, uploadFile, verifyToken,
} from '../api'
import type { Connection, Profile } from '../types'
import MultiSelect from './MultiSelect'
import RangeSlider from './RangeSlider'
import Toggle from './Toggle'

/* The 8-step new-profile wizard (design handoff: profile system UI).
   Step numbering is data-driven — when Google auth ships, the Link-account
   step is deleted from this array and everything renumbers itself. */

interface StepDef {
  key: string
  name: string
  title: string
  body: string
  skip?: string // footer text-link label; a skip is a valid finish, not a dead end
}

const STEPS: StepDef[] = [
  {
    key: 'welcome', name: 'Welcome',
    title: 'A profile is how the agent knows you',
    body: "Facts, résumé, the way you write, and what you're looking for — all kept in one profile on this machine. Several people can each have their own; one is active at a time.",
  },
  {
    key: 'account', name: 'Link account',
    title: "Link a cloud account, or don't",
    body: 'Linking syncs this profile across your devices and gives you the shared job feed. Everything works without it — your data simply stays here.',
    skip: 'Keep everything on this device',
  },
  {
    key: 'resume', name: 'Résumé',
    title: 'Start from your résumé',
    body: "It gets uploaded to every form, and your facts prefill from it. You'll review everything before anything is saved.",
    skip: 'Add it later',
  },
  {
    key: 'voice', name: 'Background & voice',
    title: 'Teach it how you write',
    body: "Three ways in — paste something you've already written, tell a story in your own words, or drop files. All of it lands in the same knowledge base.",
    skip: 'Skip for now',
  },
  {
    key: 'prefs', name: 'Preferences',
    title: 'What should it look for?',
    body: "These shape the ranking, not the gate — a role slightly outside them can still surface if it's a strong match. Hard requirements come next.",
  },
  {
    key: 'reqs', name: 'Requirements',
    title: 'The answers every form asks for',
    body: 'Unlike preferences, these are hard filters — a role that conflicts with them never reaches your feed, and the agent fills them verbatim on every application.',
  },
  {
    key: 'connections', name: 'Connections',
    title: 'Connect your tools',
    body: 'Claude Code runs the agent. Gmail lets it clear the verification codes some ATS forms send mid-application.',
    skip: 'Sort this out later',
  },
  {
    key: 'done', name: 'Done',
    title: 'Your profile is ready',
    body: 'Everything below lives on this machine. You can change any of it from the Profile page.',
  },
]

const WELCOME_ITEMS = [
  { num: '1', title: 'It fills forms as you would', desc: 'Your facts go in verbatim. Nothing is invented, and nothing is submitted without your say-so.' },
  { num: '2', title: 'It answers in your voice', desc: 'Past cover letters and stories become a knowledge base the agent draws on for open-ended questions.' },
  { num: '3', title: 'Everything stays on this device', desc: 'Linking a cloud account is optional. Local-only is fully supported.' },
]

/* Step 3 review grid — identity facts. Requirement facts live on step 6. */
const FACT_FIELDS: { key: string; label: string; full?: boolean }[] = [
  { key: 'first_name', label: 'First name' },
  { key: 'last_name', label: 'Last name' },
  { key: 'email', label: 'Email' },
  { key: 'phone', label: 'Phone' },
  { key: 'location', label: 'Location' },
  { key: 'current_title', label: 'Current title' },
  { key: 'current_company', label: 'Most recent employer' },
  { key: 'years_experience', label: 'Years of experience' },
  { key: 'linkedin_url', label: 'LinkedIn URL', full: true },
]

const SENIORITY_LEVELS = [
  'Early Career', 'Mid', 'Senior', 'Staff', 'Lead', 'Manager',
  'Associate', 'Principal', 'Director',
]

const WORK_AUTH_OPTIONS = [
  'US citizen', 'US permanent resident', 'Authorized to work in the US',
  'Requires visa sponsorship',
]

type SeniorityState = 'include' | 'exclude' | 'neutral'
type TokenState = 'idle' | 'checking' | 'linked' | 'invalid' | 'unreachable'

const INPUT_STYLE: React.CSSProperties = {
  width: '100%', border: '1px solid var(--border-3)', background: 'var(--bg-card)',
  borderRadius: 10, padding: '11px 13px', fontSize: 14, outline: 'none',
  color: 'var(--ink)',
}

const FIELD_LABEL: React.CSSProperties = {
  fontSize: 11, letterSpacing: '.05em', textTransform: 'uppercase',
  color: 'var(--text-4)', marginBottom: 6, display: 'block',
}

interface Props {
  profile: Profile
  connections: Connection[]
  gmailAccount: string | null
  onClose: () => void
  onProfileSaved: (p: Profile) => void
  goToJobs: () => void
}

export default function Onboarding({
  profile, connections, gmailAccount, onClose, onProfileSaved, goToJobs,
}: Props) {
  const stepKey = `applyer-wizard-step-${profile.profile_id}`
  const [step, setStep] = useState(() => {
    const qa = new URLSearchParams(window.location.search).get('wizard')
    const saved = qa !== null ? Number(qa) : Number(localStorage.getItem(stepKey))
    return Number.isInteger(saved) && saved >= 0 && saved < STEPS.length ? saved : 0
  })
  const cur = STEPS[step]
  useEffect(() => { localStorage.setItem(stepKey, String(step)) }, [step, stepKey])

  const [current, setCurrent] = useState(profile) // freshest server copy
  const [facts, setFacts] = useState<Record<string, string>>({ ...profile.facts })
  const [saveNote, setSaveNote] = useState('')

  /* step 2 — link account */
  const [token, setToken] = useState('')
  const [tokenState, setTokenState] = useState<TokenState>('idle')
  const [tokenDetail, setTokenDetail] = useState('')
  const [linkedEmail, setLinkedEmail] = useState('')

  /* step 3 — résumé */
  const resumeInput = useRef<HTMLInputElement>(null)
  const [resumeNote, setResumeNote] = useState('')
  const [prefill, setPrefill] = useState<Record<string, string>>({})

  /* step 4 — background & voice */
  const contextInput = useRef<HTMLInputElement>(null)
  const [contextFiles, setContextFiles] = useState(profile.context_files)
  const [rowNote, setRowNote] = useState<Record<string, string>>({})
  const [pasteText, setPasteText] = useState('')
  const [storyText, setStoryText] = useState('')
  const [voiceNote, setVoiceNote] = useState('')

  /* step 5 — preferences (criteria) */
  const [titles, setTitles] = useState<string[]>([])
  const [titleOptions, setTitleOptions] = useState<string[]>([])
  const [seniority, setSeniority] = useState<Record<string, SeniorityState>>({})
  const [locations, setLocations] = useState<string[]>([])
  const [remoteOk, setRemoteOk] = useState(true)
  const [salaryFloor, setSalaryFloor] = useState(130) // $k
  const [criteriaNote, setCriteriaNote] = useState('')

  useEffect(() => {
    fetchCriteria().then((c) => {
      setTitles(c.titles)
      setTitleOptions([...new Set([...c.search_titles, ...c.titles])])
      setLocations(c.locations)
      setRemoteOk(c.remote_ok)
      if (c.salary_floor) setSalaryFloor(Math.round(c.salary_floor / 1000))
      const s: Record<string, SeniorityState> = {}
      const levels = new Set([
        ...SENIORITY_LEVELS, ...c.acceptable_seniority, ...c.excluded_seniority,
      ])
      levels.forEach((l) => { s[l] = 'neutral' })
      c.acceptable_seniority.forEach((l) => { s[l] = 'include' })
      c.excluded_seniority.forEach((l) => { s[l] = 'exclude' })
      setSeniority(s)
    }).catch(() => {})
  }, [])

  /* step 6 — requirements */
  const [reqTouched, setReqTouched] = useState<Set<string>>(new Set())
  const touchReq = (key: string, value: string) => {
    setFacts((f) => ({ ...f, [key]: value }))
    setReqTouched((t) => new Set(t).add(key))
  }

  const gmailMismatch = Boolean(
    gmailAccount && facts.email
    && gmailAccount.trim().toLowerCase() !== facts.email.trim().toLowerCase(),
  )

  /* ---- persistence helpers -------------------------------------------- */

  const saveFacts = async (keys?: string[]) => {
    const dirty = Object.fromEntries(
      Object.entries(facts).filter(([k, v]) =>
        (keys ? keys.includes(k) : true) && (current.facts[k] ?? '') !== v),
    )
    if (!Object.keys(dirty).length) return
    const updated = await updateProfile(dirty)
    setCurrent(updated)
    onProfileSaved(updated)
  }

  const saveCriteria = async () => {
    const acceptable = Object.keys(seniority).filter((k) => seniority[k] === 'include')
    const excluded = Object.keys(seniority).filter((k) => seniority[k] === 'exclude')
    await updateCriteria({
      titles, locations,
      acceptable_seniority: acceptable, excluded_seniority: excluded,
      salary_floor: salaryFloor * 1000, remote_ok: remoteOk,
    })
  }

  const next = async () => {
    setSaveNote('')
    try {
      if (cur.key === 'resume') await saveFacts(FACT_FIELDS.map((f) => f.key))
      if (cur.key === 'prefs') { await saveCriteria(); setCriteriaNote('') }
      if (cur.key === 'reqs') await saveFacts([...reqTouched])
    } catch (e) {
      setSaveNote(String((e as Error).message))
      return
    }
    if (step >= STEPS.length - 1) { onClose(); return }
    setStep(step + 1)
  }

  const skip = () => { setSaveNote(''); setStep(Math.min(STEPS.length - 1, step + 1)) }

  /* ---- step 2 actions --------------------------------------------------- */

  const runVerify = async () => {
    if (!token.trim() || tokenState === 'checking') return
    setTokenState('checking')
    setTokenDetail('')
    try {
      const r = await verifyToken(token.trim())
      if (r.ok) {
        setTokenState('linked')
        setLinkedEmail(r.email ?? '')
        setTokenDetail(r.device_label
          ? `Linked · this device is now “${r.device_label}”` : 'Linked')
      } else if (r.reason === 'unreachable') {
        setTokenState('unreachable')
        setTokenDetail(r.detail ?? 'Applyer’s cloud service could not be reached.')
      } else {
        setTokenState('invalid')
        setTokenDetail(r.detail
          ?? "That token wasn't recognised. Tokens expire after 15 minutes — generate a fresh one and paste it again.")
      }
    } catch (e) {
      setTokenState('unreachable')
      setTokenDetail(String((e as Error).message))
    }
  }

  /* ---- step 3 actions --------------------------------------------------- */

  const onResume = async (file: File | undefined) => {
    if (!file) return
    setResumeNote('Uploading…')
    try {
      const r = await uploadFile('resume', file)
      setResumeNote(`✓ ${r.saved} uploaded${r.text_synced ? ' · text synced' : ''}`)
      const found: Record<string, string> = r.prefill ?? {}
      setPrefill(found)
      // found facts prefill EMPTY fields only — never overwrite what's typed
      setFacts((f) => {
        const out = { ...f }
        for (const [k, v] of Object.entries(found)) {
          if (!(out[k] ?? '').trim()) out[k] = v
        }
        return out
      })
    } catch (e) { setResumeNote(String((e as Error).message)) }
  }

  /* ---- step 4 actions --------------------------------------------------- */

  const addPaste = async (kind: 'pasted' | 'story') => {
    const text = (kind === 'story' ? storyText : pasteText).trim()
    if (!text) return
    setVoiceNote('')
    try {
      const title = text.split('\n')[0].slice(0, 60)
      const r = await pasteContext(text, kind, title)
      setContextFiles(r.context_files)
      setRowNote((n) => ({
        ...n,
        [r.saved]: kind === 'story' ? 'Written here · in your knowledge base' : 'Pasted · in your knowledge base',
      }))
      if (kind === 'story') setStoryText('')
      else setPasteText('')
    } catch (e) { setVoiceNote(String((e as Error).message)) }
  }

  const onContext = async (files: FileList | null) => {
    for (const f of Array.from(files ?? [])) {
      try {
        const r = await uploadFile('context', f)
        setContextFiles(r.context_files)
        setRowNote((n) => ({ ...n, [f.name]: 'Uploaded · in your knowledge base' }))
      } catch { /* per-file failure — the list simply doesn't grow */ }
    }
  }

  const removeContext = async (name: string) => {
    try {
      const r = await deleteContext(name)
      setContextFiles(r.context_files)
    } catch (e) { setVoiceNote(String((e as Error).message)) }
  }

  /* ---- derived for step 8 ----------------------------------------------- */

  const factsFilled = Object.values(current.facts).filter((v) => v.trim()).length
  const eeoPresent = current.eeo_fields_present.length > 0
  const requiredDetected = connections.filter((c) => c.required && c.connected).length
  const requiredTotal = connections.filter((c) => c.required).length
  const pct = current.completeness

  const seniorityChipStyle = (state: SeniorityState): React.CSSProperties => ({
    fontSize: 12.5, fontWeight: 600, padding: '7px 14px', borderRadius: 20,
    cursor: 'pointer', border: '1px solid var(--border-2)',
    background: state === 'include' ? 'var(--sage-soft)' : 'var(--chip)',
    color: state === 'include' ? 'var(--sage-text)'
      : state === 'exclude' ? 'var(--text-4)' : 'var(--text-2)',
    textDecoration: state === 'exclude' ? 'line-through' : 'none',
  })

  const cycleSeniority = (level: string) => {
    setSeniority((s) => ({
      ...s,
      [level]: s[level] === 'include' ? 'exclude'
        : s[level] === 'exclude' ? 'neutral' : 'include',
    }))
  }

  /* ----------------------------------------------------------------------- */

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
          <span className="serif" style={{ fontSize: 17, fontWeight: 600 }}>Set up a profile</span>
        </div>
        <button onClick={onClose} style={{
          background: 'transparent', border: 'none', color: 'var(--text-4)',
          fontSize: 22, cursor: 'pointer',
        }}>×</button>
      </div>

      <div style={{ padding: '22px 34px 0', maxWidth: 720, margin: '0 auto', width: '100%' }}>
        <div style={{ display: 'flex', gap: 8 }}>
          {STEPS.map((s, i) => (
            <div key={s.key} style={{
              flex: 1, height: 5, borderRadius: 3,
              background: i <= step ? 'var(--clay)' : 'var(--bar-track)',
            }} />
          ))}
        </div>
        <div style={{ fontSize: 12, color: 'var(--text-4)', marginTop: 10, letterSpacing: '0.03em' }}>
          Step {step + 1} of {STEPS.length} · {cur.name}
        </div>
      </div>

      <div style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', padding: '26px 34px' }}>
        <div style={{ maxWidth: 720, margin: '0 auto' }}>
          <h2 className="serif" style={{ fontSize: 29, fontWeight: 600, margin: '0 0 8px', letterSpacing: '-0.02em' }}>
            {cur.key === 'done' ? cur.title : cur.title}
          </h2>
          <p style={{ fontSize: 14, color: 'var(--text-3)', margin: '0 0 26px', lineHeight: 1.55, maxWidth: 560 }}>
            {cur.key === 'done'
              ? <>Everything below lives on this machine under{' '}
                <span style={{ fontFamily: 'ui-monospace,Menlo,monospace', fontSize: 13 }}>
                  {current.profile_dir}
                </span>. You can change any of it from the Profile page.</>
              : cur.body}
          </p>

          {/* 1 · Welcome ---------------------------------------------------- */}
          {cur.key === 'welcome' && (
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
              <div style={{ fontSize: 12.5, color: 'var(--text-4)', marginTop: 6 }}>
                Takes about four minutes. You can leave and come back.
              </div>
            </div>
          )}

          {/* 2 · Link account ----------------------------------------------- */}
          {cur.key === 'account' && (
            <div className="card" style={{
              padding: '20px 22px',
              border: tokenState === 'invalid'
                ? '1px solid var(--clay)' : '1px solid var(--border-1)',
            }}>
              <label style={FIELD_LABEL}>Personal access token</label>
              {tokenState === 'linked' ? (
                <div style={{
                  display: 'flex', alignItems: 'center', gap: 10, padding: '12px 14px',
                  background: 'var(--bg-app)', border: '1px solid var(--border-2)', borderRadius: 10,
                }}>
                  <span style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--sage)', flex: 'none' }} />
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 13.5, fontWeight: 600 }}>{linkedEmail || 'Linked'}</div>
                    <div style={{ fontSize: 12, color: 'var(--sage-text)' }}>{tokenDetail}</div>
                  </div>
                  <button className="btn-soft" style={{ padding: '7px 11px', fontSize: 12 }}
                    onClick={() => { setTokenState('idle'); setToken(''); setLinkedEmail('') }}>
                    Unlink
                  </button>
                </div>
              ) : (
                <div style={{ display: 'flex', gap: 10 }}>
                  <input
                    value={token}
                    onChange={(e) => { setToken(e.target.value); if (tokenState !== 'idle') setTokenState('idle') }}
                    onKeyDown={(e) => { if (e.key === 'Enter') runVerify() }}
                    placeholder="Paste your token — applyer_pat_…"
                    style={{
                      ...INPUT_STYLE, flex: 1,
                      fontFamily: 'ui-monospace,Menlo,monospace', fontSize: 13,
                      borderColor: tokenState === 'invalid' ? 'var(--clay)' : 'var(--border-3)',
                    }}
                  />
                  <button
                    className="btn-primary"
                    onClick={runVerify}
                    disabled={tokenState === 'checking' || !token.trim()}
                    style={tokenState === 'checking'
                      ? { background: 'var(--chip)', color: 'var(--text-3)' } : undefined}
                  >
                    {tokenState === 'checking' ? 'Checking…' : 'Verify'}
                  </button>
                </div>
              )}
              {tokenState === 'checking' && (
                <div style={{ fontSize: 12.5, color: 'var(--text-4)', marginTop: 10 }}>
                  Contacting applyer.app — this takes a few seconds.
                </div>
              )}
              {tokenState === 'invalid' && (
                <div style={{ fontSize: 12.5, color: 'var(--clay-text)', marginTop: 10, lineHeight: 1.5 }}>
                  {tokenDetail}
                </div>
              )}
              {tokenState === 'unreachable' && (
                <div style={{
                  marginTop: 12, padding: '11px 13px', borderRadius: 10,
                  background: 'var(--amber-soft)', border: '1px solid var(--amber-border)',
                  fontSize: 12.5, color: 'var(--amber-text)', lineHeight: 1.5,
                }}>
                  {tokenDetail}
                </div>
              )}
              {(tokenState === 'idle' || tokenState === 'invalid') && (
                <div style={{ fontSize: 12.5, color: 'var(--text-4)', marginTop: 12, lineHeight: 1.5 }}>
                  Sign in with Google at{' '}
                  <span style={{ fontFamily: 'ui-monospace,Menlo,monospace', fontSize: 12, color: 'var(--text-2)' }}>
                    applyer.app/device
                  </span>, copy the token it shows, paste it here. Once per device.
                </div>
              )}
            </div>
          )}

          {/* 3 · Résumé ------------------------------------------------------ */}
          {cur.key === 'resume' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              <div
                onClick={() => resumeInput.current?.click()}
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => { e.preventDefault(); onResume(e.dataTransfer.files?.[0]) }}
                style={{
                  border: '2px dashed var(--border-4)', borderRadius: 16, padding: 30,
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
                  PDF, DOCX or TXT · add a .docx to unlock per-role tailoring
                </div>
                <input ref={resumeInput} type="file" accept=".pdf,.docx,.txt" hidden
                  onChange={(e) => onResume(e.target.files?.[0])} />
              </div>

              {(current.resume_pdf || resumeNote) && (
                <div style={{
                  display: 'flex', alignItems: 'center', gap: 12, padding: '14px 16px',
                  background: 'var(--bg-card)', border: '1px solid var(--border-1)', borderRadius: 12,
                  flexWrap: 'wrap',
                }}>
                  <div style={{
                    width: 34, height: 40, borderRadius: 5, background: 'var(--clay)',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    color: 'var(--on-clay)', fontSize: 9, fontWeight: 700, flex: 'none',
                  }}>PDF</div>
                  <div style={{ flex: 1, minWidth: 160 }}>
                    <div style={{ fontSize: 13, fontWeight: 600 }}>resume.pdf</div>
                    <div style={{ fontSize: 11.5, color: 'var(--sage-text)' }}>
                      {resumeNote || '✓ Uploaded · text synced'}
                    </div>
                  </div>
                  {Object.entries(prefill).map(([k, v]) => (
                    <span key={k} style={{
                      fontSize: 11.5, fontWeight: 600, padding: '4px 10px', borderRadius: 8,
                      background: 'var(--sage-soft)', color: 'var(--sage-text)',
                    }}>
                      Found {k} · {v}
                    </span>
                  ))}
                </div>
              )}

              <div className="card" style={{ padding: '18px 20px' }}>
                <div style={{ fontSize: 13.5, fontWeight: 600, marginBottom: 2 }}>Your facts</div>
                <div style={{ fontSize: 12, color: 'var(--text-4)', marginBottom: 16 }}>
                  Edit anything that&rsquo;s off. Nothing is saved to the profile until you continue.
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '14px 20px' }}>
                  {FACT_FIELDS.map((f) => {
                    const empty = !(facts[f.key] ?? '').trim()
                    return (
                      <div key={f.key} style={f.full ? { gridColumn: '1 / -1' } : undefined}>
                        <label style={FIELD_LABEL}>{f.label}</label>
                        <input
                          value={facts[f.key] ?? ''}
                          placeholder={empty ? 'Not found — add it' : undefined}
                          onChange={(e) => setFacts({ ...facts, [f.key]: e.target.value })}
                          style={{
                            ...INPUT_STYLE,
                            borderStyle: empty ? 'dashed' : 'solid',
                          }}
                        />
                      </div>
                    )
                  })}
                </div>
                <div style={{ fontSize: 12, color: 'var(--text-4)', marginTop: 14, lineHeight: 1.5 }}>
                  Fields you leave blank fall back to your history and knowledge base.
                </div>
              </div>
            </div>
          )}

          {/* 4 · Background & voice ------------------------------------------ */}
          {cur.key === 'voice' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              <div className="card" style={{ padding: '18px 20px' }}>
                <label style={FIELD_LABEL}>Paste past answers</label>
                <textarea
                  value={pasteText}
                  onChange={(e) => setPasteText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) addPaste('pasted')
                  }}
                  rows={4}
                  placeholder='A cover letter, or an answer you&rsquo;re proud of — "Why do you want to work here?"'
                  style={{ ...INPUT_STYLE, fontSize: 13, lineHeight: 1.5 }}
                />
                <div style={{
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                  marginTop: 10,
                }}>
                  <span style={{ fontSize: 11.5, color: 'var(--text-5)' }}>
                    Ctrl↵ to add · paste as many as you like
                  </span>
                  <button className="btn-soft" style={{ padding: '7px 13px', fontSize: 12.5 }}
                    disabled={!pasteText.trim()} onClick={() => addPaste('pasted')}>
                    Add entry
                  </button>
                </div>
                <div style={{ height: 1, background: 'var(--divider)', margin: '16px 0' }} />
                <label style={FIELD_LABEL}>Tell a story</label>
                <textarea
                  value={storyText}
                  onChange={(e) => setStoryText(e.target.value)}
                  rows={3}
                  placeholder="A role or project worth knowing about — what you did, what changed."
                  style={{ ...INPUT_STYLE, fontSize: 13, lineHeight: 1.5 }}
                />
                <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 10 }}>
                  <button className="btn-soft" style={{ padding: '7px 13px', fontSize: 12.5 }}
                    disabled={!storyText.trim()} onClick={() => addPaste('story')}>
                    Add story
                  </button>
                </div>
              </div>

              <div
                onClick={() => contextInput.current?.click()}
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => { e.preventDefault(); onContext(e.dataTransfer.files) }}
                style={{
                  border: '2px dashed var(--border-4)', borderRadius: 16, padding: 20,
                  textAlign: 'center', background: 'var(--bg-app)', cursor: 'pointer',
                }}
              >
                <div style={{ fontSize: 13.5, fontWeight: 600, color: 'var(--ink)' }}>
                  Drop more documents
                </div>
                <div style={{ fontSize: 12.5, color: 'var(--text-4)', marginTop: 4 }}>
                  .md · .txt · .pdf — indexed here, never uploaded to forms
                </div>
                <input ref={contextInput} type="file" accept=".md,.txt,.pdf" multiple hidden
                  onChange={(e) => onContext(e.target.files)} />
              </div>

              {contextFiles.length > 0 && (
                <div className="card" style={{ padding: '16px 20px' }}>
                  <div style={{
                    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                    marginBottom: 10,
                  }}>
                    <span style={{ fontSize: 13, fontWeight: 600 }}>In your knowledge base</span>
                    <span className="badge-pill" style={{ background: 'var(--chip)', color: 'var(--text-3)' }}>
                      {contextFiles.length} {contextFiles.length === 1 ? 'entry' : 'entries'}
                    </span>
                  </div>
                  <div style={{ display: 'flex', flexDirection: 'column' }}>
                    {contextFiles.map((c, i) => (
                      <div key={c.name} style={{
                        display: 'flex', alignItems: 'center', gap: 10, padding: '9px 2px',
                        borderTop: i > 0 ? '1px solid var(--divider)' : 'none',
                      }}>
                        <span style={{
                          width: 7, height: 7, borderRadius: '50%', flex: 'none',
                          background: rowNote[c.name] ? 'var(--sage)' : 'var(--dot-faint)',
                        }} />
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{
                            fontSize: 12.5, fontWeight: 600, whiteSpace: 'nowrap',
                            overflow: 'hidden', textOverflow: 'ellipsis',
                          }}>{c.name}</div>
                          <div style={{ fontSize: 11, color: 'var(--text-4)' }}>
                            {rowNote[c.name] ?? c.kind}
                          </div>
                        </div>
                        <button
                          onClick={() => removeContext(c.name)}
                          style={{
                            background: 'none', border: 'none', cursor: 'pointer',
                            fontSize: 11.5, fontWeight: 600, color: 'var(--text-4)', flex: 'none',
                          }}
                        >Remove</button>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {voiceNote && (
                <div style={{ fontSize: 12.5, color: 'var(--clay-text)' }}>{voiceNote}</div>
              )}
            </div>
          )}

          {/* 5 · Preferences -------------------------------------------------- */}
          {cur.key === 'prefs' && (
            <div className="card" style={{ padding: '20px 22px' }}>
              <div>
                <div style={FIELD_LABEL}>Titles</div>
                <MultiSelect placeholder="Type a title…" options={titleOptions}
                  selected={titles} onChange={setTitles}
                  chipBg="var(--accent-soft)" chipColor="var(--clay-text)" />
              </div>
              <div style={{ height: 1, background: 'var(--divider)', margin: '20px 0' }} />
              <div>
                <div style={FIELD_LABEL}>Seniority</div>
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  {Object.keys(seniority).map((level) => (
                    <button key={level} style={seniorityChipStyle(seniority[level])}
                      onClick={() => cycleSeniority(level)}>
                      {level}{seniority[level] === 'include' ? ' ✓' : ''}
                    </button>
                  ))}
                </div>
                <div style={{ fontSize: 11.5, color: 'var(--text-5)', marginTop: 8 }}>
                  Tap once to include, twice to exclude.
                </div>
              </div>
              <div style={{ height: 1, background: 'var(--divider)', margin: '20px 0' }} />
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <div style={FIELD_LABEL}>Locations</div>
                  <span style={{
                    display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6,
                    fontSize: 12.5, fontWeight: 600, color: 'var(--text-2)',
                  }}>
                    Remote only
                    <Toggle on={!remoteOk} onChange={(v) => setRemoteOk(!v)} />
                  </span>
                </div>
                <MultiSelect placeholder="Add a city…"
                  options={['Los Angeles', 'San Francisco', 'New York', 'Remote', 'Austin', 'Seattle', 'Boston']}
                  selected={locations} onChange={setLocations}
                  chipBg="var(--purple-soft)" chipColor="var(--purple-text)" />
              </div>
              <div style={{ height: 1, background: 'var(--divider)', margin: '20px 0' }} />
              <div style={{ maxWidth: 420 }}>
                <RangeSlider label="Base salary floor" min={60} max={400} step={5}
                  value={[salaryFloor, 400]} onChange={([lo]) => setSalaryFloor(lo)} unit="k" />
                <div style={{ fontSize: 13.5, fontWeight: 600, marginTop: 6 }}>
                  ${salaryFloor}k minimum
                  <span style={{ fontSize: 11.5, fontWeight: 400, color: 'var(--text-4)', marginLeft: 8 }}>
                    disclosed pay below this is dropped
                  </span>
                </div>
              </div>
              {criteriaNote && (
                <div style={{ fontSize: 12.5, color: 'var(--clay-text)', marginTop: 12 }}>{criteriaNote}</div>
              )}
            </div>
          )}

          {/* 6 · Requirements ------------------------------------------------- */}
          {cur.key === 'reqs' && (
            <div style={{
              border: '1px solid var(--amber-border)', borderRadius: 14, overflow: 'hidden',
            }}>
              <div style={{
                background: 'var(--amber-soft)', padding: '10px 18px',
                fontSize: 12, fontWeight: 700, letterSpacing: '.05em',
                textTransform: 'uppercase', color: 'var(--amber-text)',
              }}>
                Hard requirements — these filter the job feed
              </div>
              <div style={{ background: 'var(--bg-card)', padding: '20px 22px' }}>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px 20px' }}>
                  <div>
                    <label style={FIELD_LABEL}>Work authorization</label>
                    <select
                      value={facts.work_authorization ?? ''}
                      onChange={(e) => touchReq('work_authorization', e.target.value)}
                      style={{ ...INPUT_STYLE, cursor: 'pointer' }}
                    >
                      {!(facts.work_authorization ?? '').trim() && <option value="">Choose…</option>}
                      {[...new Set([facts.work_authorization ?? '', ...WORK_AUTH_OPTIONS])]
                        .filter((o) => o.trim())
                        .map((o) => <option key={o} value={o}>{o}</option>)}
                    </select>
                  </div>
                  <div>
                    <label style={FIELD_LABEL}>Notice period</label>
                    <input value={facts.notice_period ?? ''}
                      placeholder="e.g. Two weeks"
                      onChange={(e) => touchReq('notice_period', e.target.value)}
                      style={INPUT_STYLE} />
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 11 }}>
                    <Toggle
                      on={/^y/i.test(facts.requires_sponsorship ?? '')}
                      onChange={(v) => touchReq('requires_sponsorship', v ? 'Yes' : 'No')}
                    />
                    <div>
                      <div style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--text-2)' }}>
                        Needs visa sponsorship
                      </div>
                      <div style={{ fontSize: 11.5, color: 'var(--text-4)' }}>
                        {facts.requires_sponsorship?.trim() || 'Not set'}
                      </div>
                    </div>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 11 }}>
                    <Toggle
                      on={/^y/i.test(facts.willing_to_relocate ?? '')}
                      onChange={(v) => touchReq('willing_to_relocate', v ? 'Yes' : 'No')}
                    />
                    <div>
                      <div style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--text-2)' }}>
                        Willing to relocate
                      </div>
                      <div style={{ fontSize: 11.5, color: 'var(--text-4)' }}>
                        {facts.willing_to_relocate?.trim() || 'Not set'}
                      </div>
                    </div>
                  </div>
                  <div style={{ gridColumn: '1 / -1', maxWidth: 340 }}>
                    <label style={FIELD_LABEL}>Desired base salary</label>
                    <input value={facts.desired_salary ?? ''}
                      placeholder="e.g. $185,000"
                      onChange={(e) => touchReq('desired_salary', e.target.value)}
                      style={INPUT_STYLE} />
                  </div>
                </div>
                <div style={{ fontSize: 12, color: 'var(--text-4)', marginTop: 16, lineHeight: 1.5 }}>
                  Salary here is what forms will be told. The floor on the previous
                  step only filters what you&rsquo;re shown.
                </div>
              </div>
            </div>
          )}

          {/* 7 · Connections -------------------------------------------------- */}
          {cur.key === 'connections' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {connections.map((c) => {
                const mismatchRow = c.id === 'gmail' && gmailMismatch
                const row = (
                  <div key={c.id} style={{
                    display: 'flex', alignItems: 'center', gap: 14, background: 'var(--bg-app)',
                    borderRadius: mismatchRow ? 0 : 14, padding: '15px 18px',
                    border: mismatchRow ? 'none' : '1px solid var(--border-2)',
                  }}>
                    <div style={{
                      width: 38, height: 38, borderRadius: 10, background: 'var(--accent-soft)',
                      color: 'var(--clay-text)', display: 'flex', alignItems: 'center',
                      justifyContent: 'center', fontFamily: "'Newsreader',serif",
                      fontWeight: 600, flex: 'none',
                    }}>{c.mono || monogram(c.name)}</div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <span style={{ fontSize: 14, fontWeight: 600 }}>{c.name}</span>
                        {c.required && (
                          <span className="tag" style={{ background: 'var(--chip)', color: 'var(--text-4)' }}>
                            REQUIRED
                          </span>
                        )}
                      </div>
                      <div style={{ fontSize: 12, color: 'var(--text-3)' }}>
                        {c.id === 'gmail' && gmailAccount
                          ? <>Connected as <b style={{ color: 'var(--text-2)' }}>{gmailAccount}</b></>
                          : c.short}
                      </div>
                    </div>
                    {c.connected ? (
                      <span style={{
                        display: 'flex', alignItems: 'center', gap: 6, fontSize: 12.5,
                        fontWeight: 600, color: 'var(--sage-text)',
                      }}>
                        <span style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--sage)' }} />
                        Detected
                      </span>
                    ) : (
                      <span style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--text-4)' }}>
                        Not detected
                      </span>
                    )}
                  </div>
                )
                if (!mismatchRow) return row
                /* Gmail mismatch: the row + warning share ONE amber container so
                   the warning reads as belonging to the row, not a global alert */
                return (
                  <div key={c.id} style={{
                    border: '1px solid var(--amber-border)', borderRadius: 14, overflow: 'hidden',
                  }}>
                    {row}
                    <div style={{
                      background: 'var(--amber-soft)', padding: '13px 18px',
                      fontSize: 12.5, color: 'var(--amber-text)', lineHeight: 1.55,
                    }}>
                      Verification codes will be read from <b>{gmailAccount}</b>, but
                      you&rsquo;re applying as <b>{facts.email}</b>. Codes sent to the
                      address on your applications won&rsquo;t be found.
                      <div style={{ display: 'flex', gap: 14, marginTop: 9 }}>
                        <button style={{
                          background: 'none', border: 'none', padding: 0, cursor: 'pointer',
                          fontSize: 12.5, fontWeight: 600, color: 'var(--amber-text)',
                          textDecoration: 'underline',
                        }} onClick={() => { /* authorization happens in Claude Code */ }}>
                          Connect the other account
                        </button>
                        <button style={{
                          background: 'none', border: 'none', padding: 0, cursor: 'pointer',
                          fontSize: 12.5, fontWeight: 600, color: 'var(--amber-text)',
                          textDecoration: 'underline',
                        }} onClick={async () => {
                          setFacts((f) => ({ ...f, email: gmailAccount! }))
                          try {
                            const updated = await updateProfile({ email: gmailAccount! })
                            setCurrent(updated)
                            onProfileSaved(updated)
                          } catch { /* surfaced on the next save */ }
                        }}>
                          Use {gmailAccount} on applications instead
                        </button>
                      </div>
                    </div>
                  </div>
                )
              })}
              <div style={{ fontSize: 12, color: 'var(--text-4)', lineHeight: 1.5 }}>
                Authorize connectors in Claude Code (<b>/mcp</b>) or claude.ai connector
                settings — this list reflects detected status.
              </div>
            </div>
          )}

          {/* 8 · Done ---------------------------------------------------------- */}
          {cur.key === 'done' && (
            <div style={{ display: 'flex', gap: 18, alignItems: 'stretch', flexWrap: 'wrap' }}>
              <div className="card" style={{ flex: 1, minWidth: 320, padding: '20px 22px' }}>
                <div style={{
                  fontSize: 11.5, fontWeight: 700, letterSpacing: '.05em',
                  textTransform: 'uppercase', color: 'var(--text-4)', marginBottom: 12,
                }}>Set up</div>
                {[
                  `${factsFilled} facts in your profile`,
                  `${contextFiles.length} document${contextFiles.length === 1 ? '' : 's'} in the knowledge base`,
                  `Criteria — ${titles.length} title${titles.length === 1 ? '' : 's'}, ${locations.length} location${locations.length === 1 ? '' : 's'}, $${salaryFloor}k floor`,
                  `${requiredDetected} of ${requiredTotal} required tools detected`,
                ].map((line) => (
                  <div key={line} style={{
                    display: 'flex', alignItems: 'center', gap: 10, padding: '7px 0',
                    fontSize: 13, color: 'var(--text-2)',
                  }}>
                    <span style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--sage)', flex: 'none' }} />
                    {line}
                  </div>
                ))}
                <div style={{
                  fontSize: 11.5, fontWeight: 700, letterSpacing: '.05em',
                  textTransform: 'uppercase', color: 'var(--text-4)', margin: '16px 0 12px',
                }}>Left for later</div>
                {[
                  {
                    label: 'Cloud account', amber: false,
                    action: tokenState === 'linked' ? null
                      : { text: 'link now', to: STEPS.findIndex((s) => s.key === 'account') },
                    done: tokenState === 'linked',
                  },
                  ...(gmailMismatch ? [{
                    label: 'Gmail address mismatch', amber: true,
                    action: { text: 'fix it', to: STEPS.findIndex((s) => s.key === 'connections') },
                    done: false,
                  }] : []),
                  {
                    label: 'Self-identification', amber: false,
                    action: eeoPresent ? null : null,
                    done: eeoPresent,
                    note: eeoPresent ? undefined : 'optional — add it from the Profile page',
                  },
                ].map((item) => (
                  <div key={item.label} style={{
                    display: 'flex', alignItems: 'center', gap: 10, padding: '7px 0',
                    fontSize: 13, color: 'var(--text-3)',
                  }}>
                    <span style={{
                      width: 7, height: 7, borderRadius: '50%', flex: 'none',
                      background: item.done ? 'var(--sage)'
                        : item.amber ? 'var(--amber)' : 'var(--dot-faint)',
                    }} />
                    {item.label}
                    {item.action && !item.done && (
                      <button
                        onClick={() => setStep(item.action!.to)}
                        style={{
                          background: 'none', border: 'none', padding: 0, cursor: 'pointer',
                          fontSize: 12.5, fontWeight: 600, color: 'var(--clay-text)',
                        }}
                      >{item.action.text}</button>
                    )}
                    {'note' in item && item.note && (
                      <span style={{ fontSize: 12, color: 'var(--text-5)' }}>{item.note}</span>
                    )}
                  </div>
                ))}
              </div>
              <div className="card" style={{
                width: 230, padding: '24px 20px', display: 'flex', flexDirection: 'column',
                alignItems: 'center', justifyContent: 'center', gap: 12, flex: 'none',
              }}>
                <div style={{
                  width: 104, height: 104, borderRadius: '50%',
                  background: `conic-gradient(var(--sage) 0 ${pct}%, var(--bar-track) ${pct}% 100%)`,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                }}>
                  <div style={{
                    width: 80, height: 80, borderRadius: '50%', background: 'var(--bg-card)',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                  }}>
                    <span className="serif" style={{ fontSize: 26, fontWeight: 600, color: 'var(--sage-text)' }}>
                      {pct}%
                    </span>
                  </div>
                </div>
                <div style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: 13.5, fontWeight: 600 }}>Profile complete</div>
                  <div style={{ fontSize: 12, color: 'var(--text-4)', marginTop: 3 }}>
                    Enough to start applying today.
                  </div>
                </div>
              </div>
            </div>
          )}

          {saveNote && (
            <div style={{ fontSize: 12.5, color: 'var(--clay-text)', marginTop: 14 }}>{saveNote}</div>
          )}
        </div>
      </div>

      <div style={{ padding: '16px 26px', borderTop: '1px solid var(--border-3)' }}>
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
          <div style={{ display: 'flex', alignItems: 'center', gap: 20 }}>
            {cur.skip && (
              <button onClick={skip} style={{
                background: 'none', border: 'none', cursor: 'pointer', padding: 0,
                fontSize: 13, fontWeight: 600, color: 'var(--text-3)',
                textDecoration: 'underline', textUnderlineOffset: 3,
              }}>{cur.skip}</button>
            )}
            <button
              onClick={cur.key === 'done' ? () => { onClose(); goToJobs() } : next}
              style={{
                background: 'var(--clay)', color: 'var(--on-clay)', border: 'none',
                padding: '12px 26px', borderRadius: 12, fontSize: 14, fontWeight: 600, cursor: 'pointer',
              }}
            >
              {cur.key === 'done' ? 'Find me some jobs →'
                : cur.key === 'welcome' ? "Let's go →" : 'Continue →'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
