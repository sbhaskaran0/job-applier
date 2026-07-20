import { useEffect, useRef, useState } from 'react'

interface Props {
  label: string
  min: number
  max: number
  step: number
  value: [number, number]
  onChange: (v: [number, number]) => void
  unit: 'yrs' | 'k' // 'k' renders "$ [n] k" inputs, 'yrs' renders "[n] yrs"
}

const FIELD_LABEL: React.CSSProperties = {
  fontSize: 11, letterSpacing: '.05em', textTransform: 'uppercase',
  color: 'var(--text-5)', marginBottom: 7,
}

export default function RangeSlider({ label, min, max, step, value, onChange, unit }: Props) {
  const [lo, hi] = value
  const trackRef = useRef<HTMLDivElement>(null)
  const [dragging, setDragging] = useState<0 | 1 | null>(null)

  const clampStep = (n: number) =>
    Math.min(max, Math.max(min, Math.round(n / step) * step))

  const setHandle = (idx: 0 | 1, raw: number) => {
    const v = clampStep(raw)
    if (idx === 0) onChange([Math.min(v, hi), hi])
    else onChange([lo, Math.max(v, lo)])
  }

  useEffect(() => {
    if (dragging === null) return
    const move = (e: PointerEvent) => {
      const track = trackRef.current
      if (!track) return
      const r = track.getBoundingClientRect()
      const pct = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width))
      setHandle(dragging, min + pct * (max - min))
    }
    const up = () => setDragging(null)
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
  })

  const pct = (v: number) => ((v - min) / (max - min)) * 100

  const numInput = (idx: 0 | 1) => (
    <input
      type="number" value={idx === 0 ? lo : hi} min={min} max={max} step={step}
      onChange={(e) => setHandle(idx, Number(e.target.value))}
      style={{
        width: unit === 'k' ? 48 : 42, padding: 4,
        border: '1px solid var(--border-2)', borderRadius: 7,
        background: 'var(--bg-app)', color: 'var(--ink)',
        fontSize: 12.5, textAlign: 'center',
      }}
    />
  )

  return (
    <div>
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
      }}>
        <div style={FIELD_LABEL}>{label}</div>
        <div style={{
          display: 'flex', alignItems: 'center', gap: 5, fontSize: 12.5,
          color: 'var(--text-4)', marginBottom: 7,
        }}>
          {unit === 'k' && <span>$</span>}
          {numInput(0)}
          <span>–</span>
          {unit === 'k' && <span>$</span>}
          {numInput(1)}
          <span>{unit === 'k' ? 'k' : 'yrs'}</span>
        </div>
      </div>
      <div ref={trackRef} style={{
        position: 'relative', height: 20, display: 'flex', alignItems: 'center',
      }}>
        <div style={{
          position: 'absolute', left: 0, right: 0, height: 4, borderRadius: 3,
          background: 'var(--bar-track)',
        }} />
        <div style={{
          position: 'absolute', height: 4, borderRadius: 3, background: 'var(--clay)',
          left: `${pct(lo)}%`, width: `${pct(hi) - pct(lo)}%`,
        }} />
        {([0, 1] as const).map((idx) => (
          <div key={idx}
            onPointerDown={(e) => { e.preventDefault(); setDragging(idx) }}
            style={{
              position: 'absolute', left: `${pct(idx === 0 ? lo : hi)}%`,
              transform: 'translateX(-50%)', width: 16, height: 16,
              borderRadius: '50%', background: 'var(--bg-card)',
              border: '2px solid var(--clay)', boxShadow: '0 1px 3px rgba(0,0,0,.3)',
              cursor: 'grab', touchAction: 'none',
            }}
          />
        ))}
      </div>
    </div>
  )
}
