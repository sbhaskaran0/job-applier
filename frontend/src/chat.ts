import { useCallback, useEffect, useRef, useState } from 'react'
import type { ChatMessage, RunCard } from './types'

/* One WebSocket = one Claude Code conversation. Tool calls stream in as
   run-card steps; assistant text arrives as agent bubbles. */

const GREETING: ChatMessage = {
  role: 'agent',
  text: 'Ready when you are. Ask me to find roles, queue applications, or tailor a resume — I run the real /find-jobs, /apply-batch and /tailor-application skills in this repo.',
}

export interface AgentChat {
  messages: ChatMessage[]
  typing: boolean
  connected: boolean
  send: (text: string) => void
  interrupt: () => void
}

export function useAgentChat(): AgentChat {
  const [messages, setMessages] = useState<ChatMessage[]>([GREETING])
  const [typing, setTyping] = useState(false)
  const [connected, setConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const liveRunIdx = useRef<number | null>(null)
  const currentChip = useRef('agent')
  const closedByUs = useRef(false)

  useEffect(() => {
    let retry = 0
    let timer: number | undefined
    /* Close out the live run card (if any) so no spinner outlives its turn —
       runs also end via error or a dropped session, not just "done". */
    const finalizeRun = (ok: boolean, title?: string) => {
      setMessages((m) => {
        const idx = liveRunIdx.current
        if (idx == null || !m[idx]?.run) return m
        const next = [...m]
        const run = next[idx].run as RunCard
        next[idx] = {
          ...next[idx],
          run: {
            ...run,
            title: title ?? (ok ? 'Run complete' : 'Run ended with an error'),
            steps: run.steps.map((s) => ({
              ...s,
              status: s.status === 'active' ? (ok ? 'done' : 'warn') : s.status,
            })),
          },
        }
        return next
      })
      liveRunIdx.current = null
    }
    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      const ws = new WebSocket(`${proto}://${location.host}/ws/chat`)
      wsRef.current = ws
      ws.onopen = () => { retry = 0 }
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data)
        if (msg.type === 'ready') setConnected(true)
        else if (msg.type === 'agent_text') {
          setMessages((m) => [...m, { role: 'agent', text: msg.text }])
        } else if (msg.type === 'tool') {
          const label = msg.detail ? `${msg.name} — ${msg.detail}` : msg.name
          setMessages((m) => {
            const next = [...m]
            const idx = liveRunIdx.current
            if (idx != null && next[idx]?.run) {
              const run = next[idx].run as RunCard
              next[idx] = {
                ...next[idx],
                run: {
                  ...run,
                  steps: [
                    ...run.steps.map((s) => ({ ...s, status: 'done' as const })),
                    { label, status: 'active' as const },
                  ],
                },
              }
            } else {
              liveRunIdx.current = next.length
              next.push({
                role: 'agent',
                run: {
                  chip: currentChip.current,
                  title: 'Working…',
                  steps: [{ label, status: 'active' }],
                },
              })
            }
            return next
          })
        } else if (msg.type === 'done') {
          setTyping(false)
          finalizeRun(msg.ok)
        } else if (msg.type === 'error') {
          setTyping(false)
          finalizeRun(false)
          setMessages((m) => [...m, { role: 'agent', text: `⚠ ${msg.message}` }])
        } else if (msg.type === 'restarting') {
          /* The backend declared the session dead and is closing the socket;
             onclose reconnects into a fresh one. */
          setMessages((m) => [...m, { role: 'agent', text: 'Restarting the agent session…' }])
        }
      }
      ws.onclose = () => {
        setConnected(false)
        setTyping(false)
        finalizeRun(false, 'Session ended')
        wsRef.current = null
        if (!closedByUs.current) {
          timer = window.setTimeout(connect, Math.min(30000, 1000 * 2 ** retry++))
        }
      }
    }
    connect()
    return () => {
      closedByUs.current = true
      if (timer) window.clearTimeout(timer)
      wsRef.current?.close()
    }
  }, [])

  const send = useCallback((text: string) => {
    const t = text.trim()
    if (!t || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
    currentChip.current = /\bautonomous\b/i.test(t)
      ? 'autonomous'
      : t.startsWith('/') ? t.split(/\s+/)[0] : 'agent'
    liveRunIdx.current = null
    setMessages((m) => [...m, { role: 'user', text: t }])
    setTyping(true)
    wsRef.current.send(JSON.stringify({ type: 'user', text: t }))
  }, [])

  const interrupt = useCallback(() => {
    wsRef.current?.send(JSON.stringify({ type: 'interrupt' }))
  }, [])

  return { messages, typing, connected, send, interrupt }
}
