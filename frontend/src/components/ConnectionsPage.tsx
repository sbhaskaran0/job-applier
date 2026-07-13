import { monogram } from '../api'
import type { Connection } from '../types'

export default function ConnectionsPage({ connections, note }: {
  connections: Connection[]
  note: string
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <header className="page-header">
        <h1>Connections</h1>
        <p>Authorize the tools the agent needs. Nothing runs until these are green.</p>
      </header>
      <div style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', padding: '26px 34px 60px' }}>
        <div style={{ maxWidth: 760, display: 'flex', flexDirection: 'column', gap: 14 }}>
          {connections.map((c) => (
            <div key={c.id} className="card" style={{
              display: 'flex', alignItems: 'center', gap: 16, padding: '18px 20px',
            }}>
              <div style={{
                width: 44, height: 44, borderRadius: 11, background: 'var(--bg-rail)',
                display: 'flex', alignItems: 'center', justifyContent: 'center', flex: 'none',
                fontFamily: "'Newsreader',serif", fontWeight: 600, fontSize: 17,
                color: 'var(--amber-text)',
              }}>{c.mono || monogram(c.name)}</div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
                  <span style={{ fontSize: 15, fontWeight: 600, color: 'var(--ink)' }}>{c.name}</span>
                  {c.required && (
                    <span className="tag" style={{ color: 'var(--clay-text)', background: 'var(--accent-soft)' }}>
                      REQUIRED
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginTop: 3, lineHeight: 1.45 }}>
                  {c.desc}
                </div>
              </div>
              <div style={{ flex: 'none' }}>
                {c.connected ? (
                  <span style={{
                    display: 'flex', alignItems: 'center', gap: 7, fontSize: 12.5,
                    fontWeight: 600, color: 'var(--sage-text)',
                  }}>
                    <span style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--sage)' }} />
                    Connected
                  </span>
                ) : (
                  <span style={{
                    display: 'flex', alignItems: 'center', gap: 7, fontSize: 12.5,
                    fontWeight: 600, color: 'var(--text-4)',
                  }}>
                    <span style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--dot-faint)' }} />
                    Not detected
                  </span>
                )}
              </div>
            </div>
          ))}
          <div style={{ fontSize: 12, color: 'var(--text-4)', lineHeight: 1.55, padding: '4px 2px' }}>
            {note}
          </div>
        </div>
      </div>
    </div>
  )
}
