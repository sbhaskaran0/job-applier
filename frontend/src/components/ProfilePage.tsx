import { useRef, useState } from 'react'
import { uploadFile } from '../api'
import type { Profile } from '../types'
import CloudAccountCard from './CloudAccountCard'
import EEOCard from './EEOCard'
import JobCriteriaCard from './JobCriteriaCard'

const FACT_ROWS: { label: string; value: (f: Record<string, string>) => string }[] = [
  { label: 'Full name', value: (f) => f.full_name },
  { label: 'Email', value: (f) => f.email },
  { label: 'Phone', value: (f) => f.phone },
  { label: 'Location', value: (f) => f.location },
  { label: 'Current role', value: (f) => [f.current_title, f.current_company].filter(Boolean).join(' · ') },
  { label: 'Years of experience', value: (f) => (f.years_experience ? `${f.years_experience} years` : '') },
  { label: 'Work authorization', value: (f) => f.work_authorization },
  { label: 'LinkedIn', value: (f) => (f.linkedin_url || '').replace(/^https?:\/\/(www\.)?linkedin\.com\//, '') },
  { label: 'Willing to relocate', value: (f) => f.willing_to_relocate },
  { label: 'Desired salary', value: (f) => f.desired_salary },
]

interface Props {
  profile: Profile | null
  openOnboarding: () => void
}

export default function ProfilePage({ profile, openOnboarding }: Props) {
  const [uploadNote, setUploadNote] = useState('')
  const resumeInput = useRef<HTMLInputElement>(null)

  if (!profile) {
    return (
      <div style={{ padding: 34, color: 'var(--text-4)', fontSize: 13 }}>
        Profile unavailable — is the backend running?
      </div>
    )
  }
  const pct = profile.completeness

  const onResumePicked = async (file: File | undefined) => {
    if (!file) return
    try {
      const r = await uploadFile('resume', file)
      setUploadNote(`Saved ${r.saved}${r.text_synced ? ' · text synced' : ''} ✓`)
    } catch (e) {
      setUploadNote(String((e as Error).message))
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <header className="page-header">
        <h1>Profile</h1>
        <p>The facts, résumé, and knowledge base the agent fills forms from.</p>
      </header>
      <div style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', padding: '26px 34px 60px' }}>
        <div style={{ maxWidth: 820, display: 'flex', flexDirection: 'column', gap: 22 }}>
          <div className="card" style={{ display: 'flex', alignItems: 'center', gap: 18, padding: '20px 22px' }}>
            <div style={{
              width: 56, height: 56, borderRadius: '50%', background: 'var(--clay)',
              color: 'var(--on-clay)', display: 'flex', alignItems: 'center',
              justifyContent: 'center', fontFamily: "'Newsreader',serif",
              fontWeight: 600, fontSize: 20, flex: 'none',
            }}>
              {(profile.facts.full_name?.trim() || profile.profile_id)
                .split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase()}
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <span className="serif" style={{ fontSize: 21, fontWeight: 600, letterSpacing: '-0.01em' }}>
                  {profile.facts.full_name?.trim() || profile.profile_id}
                </span>
                <span className="badge-pill" style={{ background: 'var(--sage-soft)', color: 'var(--sage-text)' }}>
                  Active
                </span>
              </div>
              <div style={{
                fontFamily: 'ui-monospace,Menlo,monospace', fontSize: 11.5,
                color: 'var(--text-4)', marginTop: 4,
              }}>
                {profile.profile_dir} · created {profile.created}
              </div>
            </div>
            <div style={{ textAlign: 'right', flex: 'none' }}>
              <div style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--text-2)', marginBottom: 6 }}>
                {pct}% complete
              </div>
              <div style={{ width: 120, height: 6, borderRadius: 3, background: 'var(--bar-track)' }}>
                <div style={{
                  width: `${pct}%`, height: '100%', borderRadius: 3, background: 'var(--sage)',
                }} />
              </div>
            </div>
            <button className="btn-soft" onClick={openOnboarding}>Edit setup</button>
          </div>

          <div className="card" style={{ padding: '22px 24px' }}>
            <div className="serif" style={{ fontSize: 16, fontWeight: 600, marginBottom: 4 }}>Personal facts</div>
            <div style={{ fontSize: 12, color: 'var(--text-4)', marginBottom: 16 }}>
              Filled verbatim into matching form fields.
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '14px 22px' }}>
              {FACT_ROWS.map((row) => (
                <div key={row.label}>
                  <div style={{
                    fontSize: 11, color: 'var(--text-5)', textTransform: 'uppercase',
                    letterSpacing: '0.05em', marginBottom: 4,
                  }}>{row.label}</div>
                  <div style={{ fontSize: 13.5, color: 'var(--ink)', fontWeight: 500, overflowWrap: 'anywhere' }}>
                    {row.value(profile.facts)?.trim() || '— (not set)'}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <EEOCard />

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 18 }}>
            <div className="card" style={{ padding: '20px 22px' }}>
              <div className="serif" style={{ fontSize: 16, fontWeight: 600, marginBottom: 14 }}>Résumé</div>
              <div style={{
                display: 'flex', alignItems: 'center', gap: 12, padding: '12px 14px',
                background: 'var(--bg-app)', border: '1px solid var(--border-1)', borderRadius: 10,
              }}>
                <div style={{
                  width: 34, height: 40, borderRadius: 5, background: 'var(--clay)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  color: 'var(--on-clay)', fontSize: 9, fontWeight: 700, flex: 'none',
                }}>{profile.resume_pdf ? 'PDF' : 'TXT'}</div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontWeight: 600 }}>
                    {profile.resume_pdf ? 'resume.pdf' : 'resume.txt'}
                  </div>
                  <div style={{ fontSize: 11.5, color: 'var(--text-4)' }}>
                    Uploaded to forms · synced to text
                  </div>
                </div>
                <button className="btn-soft" style={{ padding: '7px 11px', fontSize: 12 }}
                  onClick={() => resumeInput.current?.click()}>Replace</button>
                <input ref={resumeInput} type="file" accept=".pdf,.docx,.txt" hidden
                  onChange={(e) => onResumePicked(e.target.files?.[0])} />
              </div>
              <div style={{ fontSize: 11.5, color: 'var(--text-4)', marginTop: 10, lineHeight: 1.5 }}>
                {profile.resume_docx
                  ? <>A <b style={{ color: 'var(--text-2)' }}>resume.docx</b> base is present — per-role tailoring is enabled.</>
                  : <>Drop a <b style={{ color: 'var(--text-2)' }}>resume.docx</b> to enable per-role tailoring.</>}
                {uploadNote && <div style={{ marginTop: 6, color: 'var(--sage-text)' }}>{uploadNote}</div>}
              </div>
            </div>
            <div className="card" style={{ padding: '20px 22px' }}>
              <div className="serif" style={{ fontSize: 16, fontWeight: 600, marginBottom: 14 }}>Knowledge base</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {profile.context_files.map((c) => (
                  <div key={c.name} style={{
                    display: 'flex', alignItems: 'center', gap: 10, fontSize: 12.5, color: 'var(--text-2)',
                  }}>
                    <span style={{ width: 6, height: 6, borderRadius: 2, background: 'var(--dot-faint)', flex: 'none' }} />
                    <span style={{ flex: 1, overflowWrap: 'anywhere' }}>{c.name}</span>
                    <span style={{ color: 'var(--text-5)', fontSize: 11.5 }}>{c.kind}</span>
                  </div>
                ))}
              </div>
              <div style={{ fontSize: 11.5, color: 'var(--text-4)', marginTop: 12, lineHeight: 1.5 }}>
                Searched when crafting open-ended answers in your voice.
              </div>
            </div>
          </div>

          <JobCriteriaCard />

          <CloudAccountCard openLinkFlow={openOnboarding} />
        </div>
      </div>
    </div>
  )
}
