import { useEffect, useState } from 'react'
import { fetchAccount } from '../api'
import type { AccountStatus } from '../types'

/* Cloud account card. Today the backend always reports local-only (the hosted
   service is the M2 milestone); the linked state — email, offline pill, device
   table — is fully rendered whenever the API starts reporting it. */

interface Props {
  openLinkFlow: () => void // wizard step 2 (PAT paste)
}

export default function CloudAccountCard({ openLinkFlow }: Props) {
  const [account, setAccount] = useState<AccountStatus | null>(null)

  useEffect(() => { fetchAccount().then(setAccount).catch(() => {}) }, [])

  if (!account) return null

  return (
    <div className="card" style={{ padding: '22px 24px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <div style={{ flex: 1 }}>
          <div className="serif" style={{ fontSize: 16, fontWeight: 600 }}>Cloud account</div>
          {account.linked && (
            <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginTop: 2 }}>
              {account.email} · {account.provider ?? 'Google'}
            </div>
          )}
        </div>
        {account.linked ? (
          account.offline ? (
            <span className="badge-pill" style={{ background: 'var(--amber-soft)', color: 'var(--amber-text)' }}>
              Offline{account.last_synced ? ` — last synced ${account.last_synced}` : ''}
            </span>
          ) : (
            <span className="badge-pill" style={{ background: 'var(--sage-soft)', color: 'var(--sage-text)' }}>
              Synced
            </span>
          )
        ) : (
          <span className="badge-pill" style={{ background: 'var(--chip)', color: 'var(--text-3)' }}>
            Not linked
          </span>
        )}
      </div>

      {!account.linked && (
        <>
          <p style={{ fontSize: 13, color: 'var(--text-3)', lineHeight: 1.55, margin: '10px 0 14px' }}>
            This profile lives only on this machine. Link an account to sync it to
            your other devices and get the shared job feed — or leave it as it is.
          </p>
          <button className="btn-primary" onClick={openLinkFlow}>Link an account</button>
          {account.note && (
            <div style={{ fontSize: 11.5, color: 'var(--text-5)', marginTop: 10 }}>{account.note}</div>
          )}
        </>
      )}

      {account.linked && (
        <>
          <div style={{
            fontSize: 11, fontWeight: 700, letterSpacing: '.06em',
            textTransform: 'uppercase', color: 'var(--text-4)', margin: '18px 0 8px',
          }}>Devices</div>
          <div style={{
            border: '1px solid var(--border-2)', borderRadius: 11, overflow: 'hidden',
          }}>
            <div style={{
              display: 'grid', gridTemplateColumns: '1.3fr .8fr .8fr 70px',
              gap: 10, padding: '8px 14px', background: 'var(--bg-app)',
              fontSize: 10.5, fontWeight: 700, letterSpacing: '.06em',
              textTransform: 'uppercase', color: 'var(--text-5)',
            }}>
              <span>Device</span><span>Created</span><span>Last used</span><span />
            </div>
            {(account.devices ?? []).map((d) => (
              <div key={d.label} style={{
                display: 'grid', gridTemplateColumns: '1.3fr .8fr .8fr 70px',
                gap: 10, padding: '10px 14px', alignItems: 'center',
                borderTop: '1px solid var(--divider)', fontSize: 13,
              }}>
                <span style={{ fontWeight: 600 }}>
                  {d.label}
                  {d.current && <span style={{ color: 'var(--text-4)', fontWeight: 400 }}> · this one</span>}
                </span>
                <span style={{ color: 'var(--text-3)' }}>{d.created}</span>
                <span style={{ color: 'var(--text-3)' }}>{d.last_used}</span>
                <button style={{
                  background: 'none', border: 'none', padding: 0, cursor: 'pointer',
                  fontSize: 12.5, fontWeight: 600, textAlign: 'right',
                  color: d.current ? 'var(--text-4)' : 'var(--clay-text)',
                }}>Revoke</button>
              </div>
            ))}
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-4)', marginTop: 10, lineHeight: 1.5 }}>
            Applying still works offline. Changes queue up and sync when the
            connection is back.
          </div>
        </>
      )}
    </div>
  )
}
