import { agoHours } from '../api'
import type { Page, Profile, Status } from '../types'

const NAV: { key: Page; label: string; icon: JSX.Element }[] = [
  {
    key: 'chat', label: 'Jobs',
    icon: <path d="M21 11.5a8.38 8.38 0 0 1-8.5 8.5 8.5 8.5 0 0 1-3.9-.9L3 21l1.9-5.6A8.5 8.5 0 1 1 21 11.5z" />,
  },
  {
    key: 'postings', label: 'Postings',
    icon: <><rect x="3" y="4" width="18" height="4" rx="1" /><rect x="3" y="11" width="18" height="4" rx="1" /><rect x="3" y="18" width="12" height="3" rx="1" /></>,
  },
  {
    key: 'applications', label: 'Applications',
    icon: <><path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" /></>,
  },
  {
    key: 'profile', label: 'Profile',
    icon: <><circle cx="12" cy="8" r="4" /><path d="M4 21a8 8 0 0 1 16 0" /></>,
  },
  {
    key: 'connections', label: 'Connections',
    icon: <><path d="M9 12a3 3 0 0 0 3 3M9 9l6 6" /><circle cx="6" cy="6" r="3" /><circle cx="18" cy="18" r="3" /></>,
  },
]

interface Props {
  page: Page
  setPage: (p: Page) => void
  status: Status | null
  profile: Profile | null
  newCount: number
  pendingConnections: number
  theme: 'light' | 'dark'
  setTheme: (t: 'light' | 'dark') => void
  openOnboarding: () => void
}

export default function Sidebar({
  page, setPage, status, profile, newCount, pendingConnections,
  theme, setTheme, openOnboarding,
}: Props) {
  const name = profile?.facts.full_name ?? '—'
  const initials = name.split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase()

  return (
    <aside style={{
      width: 248, flex: 'none', background: 'var(--bg-sidebar)',
      borderRight: '1px solid var(--border-3)', display: 'flex',
      flexDirection: 'column', padding: '22px 16px',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 11, padding: '4px 8px 22px' }}>
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
          <div className="serif" style={{ fontSize: 19, fontWeight: 600, letterSpacing: '-0.01em', lineHeight: 1 }}>
            Applyer
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-4)', marginTop: 3, letterSpacing: '0.02em' }}>
            job agent · Claude Code
          </div>
        </div>
      </div>

      <nav style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
        {NAV.map((n) => {
          const active = page === n.key
          return (
            <button
              key={n.key}
              onClick={() => setPage(n.key)}
              style={{
                display: 'flex', alignItems: 'center', gap: 11, width: '100%',
                padding: '9px 11px', borderRadius: 10, border: 'none',
                cursor: 'pointer', fontSize: 13.5, fontWeight: 500, textAlign: 'left',
                background: active ? 'var(--bg-card)' : 'transparent',
                color: active ? 'var(--ink)' : 'var(--text-3)',
                boxShadow: active ? '0 1px 2px rgba(80,60,30,0.06)' : 'none',
              }}
            >
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">{n.icon}</svg>
              <span>{n.label}</span>
              {n.key === 'postings' && newCount > 0 && (
                <span style={{
                  marginLeft: 'auto', fontSize: 11, fontWeight: 700,
                  background: 'var(--sage-soft)', color: 'var(--sage-text)',
                  padding: '1px 7px', borderRadius: 8,
                }}>{newCount}</span>
              )}
              {n.key === 'connections' && pendingConnections > 0 && (
                <span style={{
                  marginLeft: 'auto', fontSize: 11, fontWeight: 700,
                  background: 'var(--accent-soft)', color: 'var(--clay-text)',
                  padding: '1px 7px', borderRadius: 8,
                }}>{pendingConnections}</span>
              )}
            </button>
          )
        })}
      </nav>

      <div style={{ marginTop: 'auto', display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div style={{
          background: 'var(--bg-app)', border: '1px solid var(--border-2)',
          borderRadius: 12, padding: '12px 13px',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span style={{
              fontSize: 11, fontWeight: 600, color: 'var(--text-4)',
              letterSpacing: '0.06em', textTransform: 'uppercase',
            }}>Refresh</span>
            <span style={{
              width: 7, height: 7, borderRadius: '50%',
              background: status?.last_refresh ? 'var(--sage)' : 'var(--dot-faint)',
              animation: 'pulseDot 2.4s infinite',
            }} />
          </div>
          <div style={{ fontSize: 12.5, color: 'var(--text-2)', marginTop: 6, lineHeight: 1.4 }}>
            Watchlist synced{' '}
            <b style={{ color: 'var(--ink)' }}>{agoHours(status?.store_age_hours ?? null)}</b>
            {' '}· {status?.new_qualifying ?? 0} new roles
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <button onClick={openOnboarding} style={{
            flex: 1, display: 'flex', alignItems: 'center', gap: 9, background: 'transparent',
            border: 'none', padding: '6px 8px', borderRadius: 9, cursor: 'pointer',
            color: 'var(--text-3)',
          }}>
            <div style={{
              width: 30, height: 30, borderRadius: '50%', background: 'var(--clay)',
              color: 'var(--on-clay)', display: 'flex', alignItems: 'center',
              justifyContent: 'center', fontWeight: 600, fontSize: 12, flex: 'none',
            }}>{initials}</div>
            <div style={{ textAlign: 'left', lineHeight: 1.2 }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--ink)' }}>{name}</div>
              <div style={{ fontSize: 11, color: 'var(--text-4)' }}>View setup</div>
            </div>
          </button>
          <button
            onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
            title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
            style={{
              background: 'transparent', border: '1px solid var(--border-2)',
              borderRadius: 9, width: 30, height: 30, cursor: 'pointer',
              color: 'var(--text-4)', fontSize: 13, flex: 'none',
            }}
          >{theme === 'dark' ? '☀' : '☾'}</button>
        </div>
      </div>
    </aside>
  )
}
