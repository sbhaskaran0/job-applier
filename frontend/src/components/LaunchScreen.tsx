import { useState } from 'react'
import type { ProfileSummary } from '../types'

/** Full-window profile selection, rendered INSTEAD of the app shell when no
 *  profile is active. Only interrupts when there is a real choice (several
 *  profiles, none selected) or none at all — a lone chosen profile resolves
 *  server-side and skips straight past this. */

function initials(name: string): string {
  return name.split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase()
}

function lastUsedLabel(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  const days = (Date.now() - d.getTime()) / 86_400_000
  if (days < 1) return 'today'
  if (days < 2) return 'yesterday'
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
}

interface Props {
  profiles: ProfileSummary[]
  onPick: (p: ProfileSummary) => void
  onNew: () => void
}

export default function LaunchScreen({ profiles, onPick, onNew }: Props) {
  const [busy, setBusy] = useState<string | null>(null)
  const mostRecent = profiles.reduce<ProfileSummary | null>(
    (best, p) => (p.last_used && (!best?.last_used || p.last_used > best.last_used) ? p : best),
    null,
  )

  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'var(--overlay-base)', zIndex: 90,
      display: 'flex', flexDirection: 'column', alignItems: 'center',
      justifyContent: 'center', animation: 'fadeUp .3s ease',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 11, marginBottom: 26 }}>
        <div style={{
          width: 34, height: 34, borderRadius: 10, background: 'var(--clay)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', flex: 'none',
        }}>
          <div style={{
            width: 13, height: 13, border: '2.5px solid var(--on-clay)',
            borderRadius: '50%', borderTopColor: 'transparent', transform: 'rotate(-45deg)',
          }} />
        </div>
        <div>
          <div className="serif" style={{ fontSize: 19, fontWeight: 600, lineHeight: 1 }}>Applyer</div>
          <div style={{ fontSize: 11, color: 'var(--text-4)', marginTop: 3 }}>job agent · Claude Code</div>
        </div>
      </div>

      <h2 className="serif" style={{ fontSize: 29, fontWeight: 600, margin: 0, letterSpacing: '-0.02em' }}>
        Who&rsquo;s applying?
      </h2>
      <p style={{
        fontSize: 14, color: 'var(--text-3)', margin: '8px 0 30px', lineHeight: 1.55,
        maxWidth: 460, textAlign: 'center',
      }}>
        Pick a profile to work as. Everything — postings, applications, résumé —
        is scoped to whoever you choose.
      </p>

      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', justifyContent: 'center', maxWidth: 700 }}>
        {profiles.map((p) => {
          const recent = mostRecent?.id === p.id
          return (
            <button
              key={p.id}
              disabled={busy !== null}
              onClick={() => { setBusy(p.id); onPick(p) }}
              style={{
                width: 190, background: 'var(--bg-card)', borderRadius: 16,
                padding: '22px 18px', textAlign: 'center', cursor: 'pointer',
                border: `1px solid ${recent ? 'var(--clay)' : 'var(--border-1)'}`,
                opacity: busy && busy !== p.id ? 0.55 : 1,
              }}
            >
              <div style={{
                width: 60, height: 60, borderRadius: '50%', margin: '0 auto 12px',
                background: recent ? 'var(--clay)' : 'var(--accent-soft)',
                color: recent ? 'var(--on-clay)' : 'var(--clay-text)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                fontFamily: "'Newsreader',serif", fontWeight: 600, fontSize: 21,
              }}>{initials(p.name)}</div>
              <div style={{ fontSize: 14.5, fontWeight: 600, color: 'var(--ink)' }}>
                {busy === p.id ? 'Opening…' : p.name}
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-4)', marginTop: 4 }}>
                {p.applications} application{p.applications === 1 ? '' : 's'} · {p.completeness}% set up
              </div>
              <div style={{
                fontSize: 11.5, marginTop: 8,
                color: recent ? 'var(--sage-text)' : 'var(--text-5)',
              }}>
                Last used · {lastUsedLabel(p.last_used)}
              </div>
            </button>
          )
        })}

        <button
          onClick={onNew}
          disabled={busy !== null}
          style={{
            width: 190, background: 'transparent', borderRadius: 16,
            padding: '22px 18px', textAlign: 'center', cursor: 'pointer',
            border: '2px dashed var(--border-4)',
          }}
        >
          <div style={{
            width: 60, height: 60, borderRadius: '50%', margin: '0 auto 12px',
            border: '2px dashed var(--border-4)', color: 'var(--text-4)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 26,
          }}>+</div>
          <div style={{ fontSize: 14.5, fontWeight: 600, color: 'var(--text-2)' }}>New profile</div>
          <div style={{ fontSize: 12, color: 'var(--text-4)', marginTop: 4 }}>Four-minute setup</div>
        </button>
      </div>

      <div style={{ fontSize: 12.5, color: 'var(--text-4)', marginTop: 34 }}>
        Nothing leaves this machine unless you link an account.
      </div>
    </div>
  )
}
