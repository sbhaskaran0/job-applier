import { formatSalary, monogram } from '../api'
import type { Posting } from '../types'
import { WarnIcon } from './ChatPage'

/* Preview heuristic only — real inline-vs-crafting routing happens at run
   time from snapshot_job's freetext_count. */
const LIKELY_CRAFTING = /operations|strategy|chief/i

interface Props {
  jobs: Posting[]
  autonomous: boolean
  onClose: () => void
  onConfirm: () => void
}

export default function ApplyModal({ jobs, autonomous, onClose, onConfirm }: Props) {
  return (
    <div onClick={onClose} style={{
      position: 'fixed', inset: 0, background: 'rgba(43,39,35,0.42)', display: 'flex',
      alignItems: 'center', justifyContent: 'center', zIndex: 60, animation: 'fadeUp .2s ease',
    }}>
      <div onClick={(e) => e.stopPropagation()} style={{
        width: 520, maxWidth: '92vw', background: 'var(--bg-app)', borderRadius: 20,
        overflow: 'hidden', boxShadow: '0 24px 60px rgba(43,39,35,0.35)',
      }}>
        <div style={{
          padding: '22px 24px 18px', borderBottom: '1px solid var(--border-1)',
          background: autonomous ? 'var(--amber-header)' : 'var(--bg-rail)',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 11 }}>
            <div style={{
              width: 38, height: 38, borderRadius: 11, display: 'flex', alignItems: 'center',
              justifyContent: 'center', flex: 'none',
              background: autonomous ? 'var(--amber-soft)' : 'var(--accent-soft)',
            }}>
              {autonomous ? <WarnIcon size={20} /> : (
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--clay-text)"
                  strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M9 11l3 3L22 4" />
                  <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
                </svg>
              )}
            </div>
            <div>
              <div className="serif" style={{ fontSize: 19, fontWeight: 600, lineHeight: 1.1 }}>
                {autonomous ? 'Run autonomously?' : `Queue ${jobs.length} application${jobs.length === 1 ? '' : 's'}`}
              </div>
              <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginTop: 2 }}>
                {autonomous ? 'Hands-off — fill and auto-submit' : 'One review, then serial fill'}
              </div>
            </div>
          </div>
        </div>
        <div style={{ padding: '20px 24px' }}>
          <div className="card" style={{
            borderRadius: 12, padding: '6px 16px', marginBottom: 18,
            maxHeight: 200, overflowY: 'auto', overflowX: 'hidden',
          }}>
            {jobs.map((j) => {
              const crafting = LIKELY_CRAFTING.test(j.title)
              return (
                <div key={j.url} style={{
                  display: 'flex', alignItems: 'center', gap: 11, padding: '11px 0',
                  borderTop: '1px solid var(--divider)',
                }}>
                  <div className="mono-tile" style={{ width: 30, height: 30, fontSize: 12 }}>
                    {monogram(j.company)}
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 600 }}>{j.title}</div>
                    <div style={{ fontSize: 11.5, color: 'var(--text-4)' }}>
                      {j.company} · {formatSalary(j)}
                    </div>
                  </div>
                  <span style={{
                    fontSize: 10.5, fontWeight: 700, padding: '2px 8px', borderRadius: 6,
                    letterSpacing: '0.04em',
                    background: crafting ? 'var(--amber-soft)' : 'var(--chip)',
                    color: crafting ? 'var(--amber-text)' : 'var(--text-3)',
                  }}>{crafting ? 'crafting' : 'inline'}</span>
                </div>
              )
            })}
          </div>
          {autonomous ? (
            <div style={{
              display: 'flex', gap: 11, background: 'var(--amber-soft)',
              border: '1px solid var(--amber-border)', borderRadius: 12,
              padding: '13px 15px', marginBottom: 6,
            }}>
              <WarnIcon />
              <div style={{ fontSize: 12.5, color: 'var(--amber-text)', lineHeight: 1.5 }}>
                The agent will fill every field and <b>submit without pausing</b> where it can
                verify success. Spam-flagged or CAPTCHA-gated forms are left filled for your
                one-click finish. This can't be undone once submitted.
              </div>
            </div>
          ) : (
            <div style={{ fontSize: 12.5, color: 'var(--text-3)', lineHeight: 1.55, padding: '0 2px' }}>
              Answers are prepared in parallel. You'll get{' '}
              <b style={{ color: 'var(--text-2)' }}>one consolidated review</b> — approve or edit
              every gated answer and give per-job submit consent — before anything is sent.
              Nothing submits without your say-so.
            </div>
          )}
        </div>
        <div style={{ display: 'flex', gap: 11, padding: '16px 24px 22px' }}>
          <button onClick={onClose} style={{
            flex: 'none', background: 'transparent', color: 'var(--text-3)',
            border: '1px solid var(--border-2)', padding: '11px 20px', borderRadius: 11,
            fontSize: 13.5, fontWeight: 600, cursor: 'pointer',
          }}>Cancel</button>
          <button onClick={onConfirm} style={{
            flex: 1, border: 'none', padding: '11px 20px', borderRadius: 11, fontSize: 13.5,
            fontWeight: 600, cursor: 'pointer', color: 'var(--on-clay)',
            background: autonomous ? 'var(--amber)' : 'var(--clay)',
          }}>{autonomous ? 'Yes, run autonomously' : 'Prepare & review'}</button>
        </div>
      </div>
    </div>
  )
}
