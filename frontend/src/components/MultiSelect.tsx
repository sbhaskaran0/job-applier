import { useRef, useState } from 'react'

interface Props {
  placeholder: string
  options: string[]
  selected: string[]
  onChange: (v: string[]) => void
  chipBg: string
  chipColor: string
  allowFree?: boolean
}

export default function MultiSelect({
  placeholder, options, selected, onChange, chipBg, chipColor, allowFree = true,
}: Props) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const closeTimer = useRef<number | undefined>(undefined)

  const suggestions = options
    .filter((o) => !selected.includes(o))
    .filter((o) => o.toLowerCase().includes(query.toLowerCase()))
    .slice(0, 8)

  const add = (value: string) => {
    const v = value.trim()
    if (!v || selected.includes(v)) return
    onChange([...selected, v])
    setQuery('')
  }

  const remove = (value: string) => onChange(selected.filter((s) => s !== value))

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      if (allowFree && query.trim()) add(query)
      else if (suggestions.length) add(suggestions[0])
    } else if (e.key === 'Backspace' && !query && selected.length) {
      remove(selected[selected.length - 1])
    }
  }

  return (
    <div style={{ position: 'relative' }}>
      <div style={{
        display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center',
        padding: '7px 9px', minHeight: 42, border: '1px solid var(--border-2)',
        borderRadius: 11, background: 'var(--bg-app)',
      }}>
        {selected.map((s) => (
          <span key={s} style={{
            display: 'inline-flex', alignItems: 'center', gap: 5,
            fontSize: 12.5, fontWeight: 600, padding: '4px 5px 4px 10px',
            borderRadius: 8, background: chipBg, color: chipColor,
          }}>
            {s}
            <button onClick={() => remove(s)} style={{
              background: 'none', border: 'none', cursor: 'pointer',
              color: 'inherit', fontSize: 15, padding: '0 3px', lineHeight: 1,
            }}>×</button>
          </span>
        ))}
        <input
          value={query}
          placeholder={selected.length ? '' : placeholder}
          onChange={(e) => { setQuery(e.target.value); setOpen(true) }}
          onFocus={() => { window.clearTimeout(closeTimer.current); setOpen(true) }}
          onBlur={() => { closeTimer.current = window.setTimeout(() => setOpen(false), 130) }}
          onKeyDown={onKeyDown}
          style={{
            border: 'none', background: 'transparent', outline: 'none',
            flex: 1, minWidth: 130, fontSize: 13, padding: 4, color: 'var(--ink)',
          }}
        />
      </div>
      {open && (suggestions.length > 0 || (allowFree && query.trim())) && (
        <div style={{
          position: 'absolute', top: 'calc(100% + 6px)', left: 0, right: 0,
          zIndex: 40, background: 'var(--bg-card)', border: '1px solid var(--border-2)',
          borderRadius: 11, boxShadow: '0 12px 34px rgba(0,0,0,0.28)',
          padding: 6, maxHeight: 236, overflow: 'auto',
        }}>
          {suggestions.map((o) => (
            <div key={o} onMouseDown={() => add(o)} style={{
              display: 'flex', alignItems: 'center', gap: 8, fontSize: 13,
              color: 'var(--text-2)', padding: '8px 10px', borderRadius: 8,
              cursor: 'pointer',
            }}
              onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--chip)' }}
              onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
                stroke="var(--text-4)" strokeWidth="2" strokeLinecap="round">
                <path d="M12 5v14M5 12h14" />
              </svg>
              {o}
            </div>
          ))}
          {allowFree && query.trim() && (
            <div onMouseDown={() => add(query)} style={{
              fontSize: 12.5, color: 'var(--text-4)', padding: '8px 10px',
              borderRadius: 8, cursor: 'pointer',
            }}>
              Press Enter to add “{query.trim()}”
            </div>
          )}
        </div>
      )}
    </div>
  )
}
