interface Props {
  on: boolean
  onChange: (v: boolean) => void
}

export default function Toggle({ on, onChange }: Props) {
  return (
    <button onClick={() => onChange(!on)} style={{
      position: 'relative', width: 46, height: 26, borderRadius: 13,
      padding: 0, border: 'none', cursor: 'pointer', flex: 'none',
      background: on ? 'var(--sage)' : 'var(--border-4)',
      transition: 'background .18s',
    }}>
      <span style={{
        position: 'absolute', top: 3, left: on ? 23 : 3, width: 20, height: 20,
        borderRadius: '50%', background: '#fff',
        boxShadow: '0 1px 3px rgba(0,0,0,.3)', transition: 'left .18s',
      }} />
    </button>
  )
}
