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
  /* Sequenced prompts ("Up next"): see enqueue() below. */
  queue: string[]
  enqueue: (prompts: string[]) => void
  continueQueue: () => void
  clearQueue: () => void
}

export function useAgentChat(): AgentChat {
  const [messages, setMessages] = useState<ChatMessage[]>([GREETING])
  const [typing, setTyping] = useState(false)
  const [connected, setConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const liveRunIdx = useRef<number | null>(null)
  const currentChip = useRef('agent')
  const closedByUs = useRef(false)
  const [queue, setQueue] = useState<string[]>([])
  const typingRef = useRef(false)
  const queueRef = useRef<string[]>([])

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

  useEffect(() => { typingRef.current = typing }, [typing])

  const send = useCallback((text: string) => {
    const t = text.trim()
    if (!t || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
    typingRef.current = true
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

  /* Sequenced prompts. The first prompt goes out immediately (if the agent is
     idle); the rest wait in `queue`, shown as an "Up next" list, and are
     released ONLY by an explicit continueQueue() ("Continue" button), never
     automatically. Why: /tailor-application pauses for the user's approval of
     the cover letter, so the turn ending is NOT proof the user has approved and
     finished with that role: the approval reply is itself a later turn. There is
     no reliable "agent is waiting on the user" signal in the stream, so we
     prefer correctness over automation. Continue is only enabled while the agent
     is idle (not typing), so a prompt can never interleave with a running turn. */
  const setQ = useCallback((next: string[]) => {
    queueRef.current = next
    setQueue(next)
  }, [])

  const enqueue = useCallback((prompts: string[]) => {
    const list = prompts.map((p) => p.trim()).filter(Boolean)
    if (!list.length) return
    if (!typingRef.current && wsRef.current?.readyState === WebSocket.OPEN) {
      send(list[0])
      setQ([...queueRef.current, ...list.slice(1)])
    } else {
      setQ([...queueRef.current, ...list])
    }
  }, [send, setQ])

  const continueQueue = useCallback(() => {
    const q = queueRef.current
    if (typingRef.current || !q.length) return
    send(q[0])
    setQ(q.slice(1))
  }, [send, setQ])

  const clearQueue = useCallback(() => setQ([]), [setQ])

  return { messages, typing, connected, send, interrupt, queue, enqueue, continueQueue, clearQueue }
}
