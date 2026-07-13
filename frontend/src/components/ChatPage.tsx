import { useEffect, useRef, useState } from 'react'
import { addWatchlistCompany, monogram } from '../api'
import type { AgentChat } from '../chat'
import type { ChatMessage, WatchlistCompany } from '../types'

const CHIPS = ['/find-jobs ', '/apply-batch ', '/tailor-application ', 'add to watchlist ']
const CHIP_LABELS = ['/find-jobs', '/apply-batch', '/tailor-application', 'update watchlist']
const AUTONOMOUS_COMMANDS = /^\/(find-jobs|apply-to-job|apply-batch)\s/

interface Props {
  chat: AgentChat
  autonomous: boolean
  setAutonomous: (v: boolean) => void
  watchlist: WatchlistCompany[]
  refreshWatchlist: () => void
}

export default function ChatPage({
  chat, autonomous, setAutonomous, watchlist, refreshWatchlist,
}: Props) {
  const [input, setInput] = useState('')
  const [watchInput, setWatchInput] = useState('')
  const [watchError, setWatchError] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [chat.messages, chat.typing])

  const send = () => {
    let t = input.trim()
    if (!t) return
    if (autonomous && AUTONOMOUS_COMMANDS.test(t) && !/\bautonomous\b/i.test(t)) {
      t = t.replace(/^(\/\S+)\s/, '$1 autonomous ')
    }
    chat.send(t)
    setInput('')
    if (taRef.current) taRef.current.style.height = 'auto'
  }

  const addWatch = async () => {
    const v = watchInput.trim()
    if (!v) return
    setWatchError('')
    try {
      await addWatchlistCompany(v)
      setWatchInput('')
      refreshWatchlist()
    } catch (e) {
      setWatchError(String((e as Error).message))
    }
  }

  return (
    <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
      <section style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
        <header style={{
          padding: '20px 30px 16px', borderBottom: '1px solid var(--border-1)',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <div>
            <h1 className="serif" style={{ fontWeight: 600, fontSize: 23, margin: 0, letterSpacing: '-0.015em' }}>Jobs</h1>
            <p style={{ margin: '3px 0 0', fontSize: 13, color: 'var(--text-3)' }}>
              Talk to the agent — it searches, tailors, and applies through Claude Code.
            </p>
          </div>
          <div style={{
            display: 'flex', alignItems: 'center', gap: 7, background: 'var(--chip)',
            border: '1px solid var(--border-2)', padding: '6px 11px', borderRadius: 20,
          }}>
            <span style={{
              width: 7, height: 7, borderRadius: '50%',
              background: chat.connected ? 'var(--sage)' : 'var(--dot-faint)',
            }} />
            <span style={{ fontSize: 12, color: 'var(--text-2)', fontWeight: 500 }}>
              {chat.connected ? 'Claude Code connected' : 'Connecting…'}
            </span>
          </div>
        </header>

        <div ref={scrollRef} style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', padding: '26px 30px 8px' }}>
          <div style={{ maxWidth: 720, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 18 }}>
            {chat.messages.map((m, i) => <Message key={i} m={m} />)}
            {chat.typing && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '4px 2px' }}>
                {[0, 0.2, 0.4].map((d) => (
                  <span key={d} style={{
                    width: 8, height: 8, borderRadius: '50%', background: 'var(--dot-faint)',
                    animation: `blink 1s infinite ${d}s`,
                  }} />
                ))}
              </div>
            )}
          </div>
        </div>

        <div style={{ padding: '14px 30px 20px' }}>
          <div style={{ maxWidth: 720, margin: '0 auto' }}>
            <div style={{ display: 'flex', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
              {CHIPS.map((c, i) => (
                <button key={c} onClick={() => { setInput(c); taRef.current?.focus() }} style={{
                  fontSize: 12.5, color: 'var(--text-2)', background: 'var(--chip)',
                  border: '1px solid var(--border-2)', padding: '6px 12px',
                  borderRadius: 20, cursor: 'pointer', fontWeight: 500,
                }}>{CHIP_LABELS[i]}</button>
              ))}
              {chat.typing && (
                <button onClick={chat.interrupt} style={{
                  fontSize: 12.5, color: 'var(--clay-text)', background: 'var(--accent-soft)',
                  border: '1px solid var(--border-2)', padding: '6px 12px',
                  borderRadius: 20, cursor: 'pointer', fontWeight: 600, marginLeft: 'auto',
                }}>■ Stop</button>
              )}
            </div>
            <div style={{
              display: 'flex', alignItems: 'flex-end', gap: 10, background: 'var(--bg-card)',
              border: '1px solid var(--border-3)', borderRadius: 16,
              padding: '10px 10px 10px 16px', boxShadow: '0 2px 8px rgba(80,60,30,0.05)',
            }}>
              <textarea
                ref={taRef} value={input} rows={1}
                placeholder="Ask the agent to find or apply to roles…"
                onChange={(e) => {
                  setInput(e.target.value)
                  e.target.style.height = 'auto'
                  e.target.style.height = `${Math.min(120, e.target.scrollHeight)}px`
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
                }}
                style={{
                  flex: 1, border: 'none', outline: 'none', background: 'transparent',
                  fontSize: 14.5, color: 'var(--ink)', lineHeight: 1.5,
                  padding: '5px 0', maxHeight: 120, overflow: 'hidden',
                }}
              />
              <label style={{
                display: 'flex', alignItems: 'center', gap: 7, padding: '6px 10px',
                borderRadius: 20, cursor: 'pointer', marginBottom: 2,
                background: autonomous ? 'var(--amber-soft)' : 'var(--bg-rail)',
                border: `1px solid ${autonomous ? 'var(--amber-border)' : 'var(--border-2)'}`,
                color: autonomous ? 'var(--amber-text)' : 'var(--text-4)',
              }}>
                <input type="checkbox" checked={autonomous}
                  onChange={(e) => setAutonomous(e.target.checked)}
                  style={{ accentColor: 'var(--amber)', width: 14, height: 14, cursor: 'pointer' }} />
                <span style={{ fontSize: 12, fontWeight: 600 }}>Autonomous</span>
              </label>
              <button onClick={send} style={{
                width: 38, height: 38, borderRadius: 11, background: 'var(--clay)',
                border: 'none', cursor: 'pointer', display: 'flex',
                alignItems: 'center', justifyContent: 'center', flex: 'none',
              }}>
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="var(--on-clay)"
                  strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 19V5M5 12l7-7 7 7" />
                </svg>
              </button>
            </div>
            {autonomous && (
              <div style={{
                marginTop: 9, display: 'flex', alignItems: 'center', gap: 8, fontSize: 12,
                color: 'var(--amber-text)', background: 'var(--amber-soft)',
                border: '1px solid var(--amber-border)', padding: '7px 12px', borderRadius: 10,
              }}>
                <WarnIcon size={14} />
                <span>
                  Autonomous mode will fill <b>and auto-submit</b> where it safely can,
                  without pausing for approval. You'll confirm once before it runs.
                </span>
              </div>
            )}
          </div>
        </div>
      </section>

      {/* watchlist rail */}
      <aside style={{
        width: 296, flex: 'none', borderLeft: '1px solid var(--border-1)',
        background: 'var(--bg-rail)', display: 'flex', flexDirection: 'column',
      }}>
        <div style={{ padding: '20px 20px 14px', borderBottom: '1px solid var(--border-1)' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <h2 className="serif" style={{ fontSize: 17, fontWeight: 600, margin: 0 }}>Watchlist</h2>
            <span style={{ fontSize: 12, color: 'var(--text-4)' }}>{watchlist.length} companies</span>
          </div>
          <p style={{ margin: '4px 0 0', fontSize: 12, color: 'var(--text-3)', lineHeight: 1.45 }}>
            The universe <span style={{ fontFamily: 'ui-monospace,monospace', fontSize: 11 }}>/find-jobs</span> searches.
          </p>
          <div style={{ display: 'flex', gap: 7, marginTop: 12 }}>
            <input
              value={watchInput}
              onChange={(e) => setWatchInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') addWatch() }}
              placeholder="Add board URL or company…"
              style={{
                flex: 1, border: '1px solid var(--border-3)', background: 'var(--bg-card)',
                borderRadius: 9, padding: '8px 11px', fontSize: 12.5, outline: 'none',
                color: 'var(--ink)',
              }}
            />
            <button onClick={addWatch} style={{
              background: 'var(--clay)', color: 'var(--on-clay)', border: 'none',
              borderRadius: 9, width: 34, fontSize: 18, cursor: 'pointer', flex: 'none',
            }}>+</button>
          </div>
          {watchError && (
            <div style={{ fontSize: 11.5, color: 'var(--clay-text)', marginTop: 7 }}>{watchError}</div>
          )}
        </div>
        <div style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', padding: '10px 12px' }}>
          {watchlist.map((w) => (
            <div key={`${w.ats}/${w.slug}`} style={{
              display: 'flex', alignItems: 'center', gap: 11, padding: '9px 10px',
              borderRadius: 10, marginBottom: 2,
            }}>
              <div style={{
                width: 30, height: 30, borderRadius: 8, background: 'var(--bg-card)',
                border: '1px solid var(--border-1)', display: 'flex', alignItems: 'center',
                justifyContent: 'center', fontFamily: "'Newsreader',serif", fontWeight: 600,
                fontSize: 13, color: 'var(--amber-text)', flex: 'none',
              }}>{monogram(w.name)}</div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{
                  fontSize: 13, fontWeight: 600, color: 'var(--ink)', whiteSpace: 'nowrap',
                  overflow: 'hidden', textOverflow: 'ellipsis',
                }}>{w.name}</div>
                <div style={{ fontSize: 11, color: 'var(--text-4)' }}>{w.ats} · {w.active} active</div>
              </div>
              <span style={{
                fontSize: 12, fontWeight: 700, padding: '3px 8px', borderRadius: 8, flex: 'none',
                background: w.qualifying > 0 ? 'var(--sage-soft)' : 'var(--chip)',
                color: w.qualifying > 0 ? 'var(--sage-text)' : 'var(--text-5)',
              }}>{w.qualifying}</span>
            </div>
          ))}
        </div>
      </aside>
    </div>
  )
}

function Message({ m }: { m: ChatMessage }) {
  const isUser = m.role === 'user'
  return (
    <div style={{
      display: 'flex', flexDirection: 'column',
      alignItems: isUser ? 'flex-end' : 'flex-start', gap: 8,
    }}>
      {m.text && (
        <div style={isUser ? {
          background: 'var(--clay)', color: 'var(--on-clay)', padding: '11px 16px',
          borderRadius: '16px 16px 4px 16px', lineHeight: 1.5, maxWidth: '78%',
          fontFamily: 'ui-monospace,SFMono-Regular,monospace', fontSize: 13,
          overflowWrap: 'anywhere', wordBreak: 'break-word',
        } : {
          background: 'var(--bg-card)', border: '1px solid var(--border-1)',
          color: 'var(--ink)', padding: '12px 16px', borderRadius: '16px 16px 16px 4px',
          fontSize: 14, lineHeight: 1.55, maxWidth: '82%', whiteSpace: 'pre-wrap',
          overflowWrap: 'anywhere', animation: 'fadeUp .25s ease',
        }}>{m.text}</div>
      )}
      {m.run && (
        <div style={{
          background: 'var(--bg-card)', border: '1px solid var(--border-1)',
          borderRadius: 14, padding: '16px 18px', width: '100%',
          boxShadow: '0 1px 2px rgba(80,60,30,0.04)', animation: 'fadeUp .3s ease',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 12 }}>
            <span style={{
              background: m.run.chip === 'autonomous' ? 'var(--amber-soft)' : 'var(--chip)',
              color: m.run.chip === 'agent' ? 'var(--text-3)' : 'var(--amber-text)',
              fontSize: 11, fontWeight: 600, padding: '3px 9px', borderRadius: 20,
              letterSpacing: '0.03em',
            }}>{m.run.chip}</span>
            <span className="serif" style={{ fontSize: 16, fontWeight: 600 }}>{m.run.title}</span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 9 }}>
            {m.run.steps.map((s, i) => {
              const color = s.status === 'done' ? 'var(--sage)'
                : s.status === 'warn' ? 'var(--amber)'
                : s.status === 'active' ? 'var(--clay)' : 'var(--dot-faint)'
              return (
                <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <span style={{
                    width: 9, height: 9, borderRadius: '50%', flex: 'none', background: color,
                    animation: s.status === 'active' ? 'pulseDot 1.3s infinite' : undefined,
                  }} />
                  <span style={{
                    fontSize: 13,
                    color: s.status === 'active' ? 'var(--ink)' : 'var(--text-2)',
                    fontWeight: s.status === 'active' ? 600 : 400,
                    overflowWrap: 'anywhere',
                  }}>{s.label}</span>
                </div>
              )
            })}
          </div>
          {m.run.footer && (
            <div style={{
              marginTop: 13, paddingTop: 12, borderTop: '1px solid var(--chip)',
              fontSize: 12.5, color: 'var(--text-3)',
            }}>{m.run.footer}</div>
          )}
        </div>
      )}
    </div>
  )
}

export function WarnIcon({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="var(--amber)"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flex: 'none' }}>
      <path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
    </svg>
  )
}
