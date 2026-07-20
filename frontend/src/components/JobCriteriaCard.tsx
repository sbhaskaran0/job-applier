import { useEffect, useRef, useState } from 'react'
import { fetchCriteria, updateCriteria } from '../api'
import type { Criteria } from '../types'
import MultiSelect from './MultiSelect'
import RangeSlider from './RangeSlider'
import Toggle from './Toggle'

const ACCEPTABLE_SENIORITY = [
  'Early Career', 'Mid', 'Senior', 'Lead', 'Manager', 'Staff', 'Associate',
]
const EXCLUDED_SENIORITY = [
  'Director', 'VP', 'Head', 'Principal', 'Intern', 'Junior', 'Founder',
]
const POSTED_WITHIN = [7, 14, 30, 60]

const DEFAULTS = {
  titles: ['Product Manager', 'Senior Product Manager', 'Product Strategy',
    'Business Operations', 'Chief of Staff'],
  locations: ['Los Angeles', 'Remote'],
  acceptable: ['Early Career', 'Mid', 'Senior'],
  excluded: ['Director', 'VP', 'Head', 'Principal', 'Intern', 'Junior'],
  yoe: [2, 12] as [number, number],
  salary: [130, 300] as [number, number],
  days: 30,
  remoteOk: true,
}

const FIELD_LABEL: React.CSSProperties = {
  fontSize: 11, letterSpacing: '.05em', textTransform: 'uppercase',
  color: 'var(--text-5)', marginBottom: 7,
}

export default function JobCriteriaCard() {
  const [titles, setTitles] = useState<string[]>(DEFAULTS.titles)
  const [locations, setLocations] = useState<string[]>(DEFAULTS.locations)
  const [acceptable, setAcceptable] = useState<string[]>(DEFAULTS.acceptable)
  const [excluded, setExcluded] = useState<string[]>(DEFAULTS.excluded)
  const [yoe, setYoe] = useState<[number, number]>(DEFAULTS.yoe)
  const [salary, setSalary] = useState<[number, number]>(DEFAULTS.salary)
  const [days, setDays] = useState(DEFAULTS.days)
  const [remoteOk, setRemoteOk] = useState(DEFAULTS.remoteOk)
  const [titleOptions, setTitleOptions] = useState<string[]>(DEFAULTS.titles)
  const [savedToast, setSavedToast] = useState(false)
  const [saveError, setSaveError] = useState('')
  const toastTimer = useRef<number | undefined>(undefined)

  const applyCriteria = (c: Criteria) => {
    setTitles(c.titles)
    setLocations(c.locations)
    setAcceptable(c.acceptable_seniority)
    setExcluded(c.excluded_seniority)
    setYoe(c.yoe)
    setSalary([Math.round((c.salary_floor ?? 130000) / 1000), DEFAULTS.salary[1]])
    setDays(c.date_posted_days ?? DEFAULTS.days)
    setRemoteOk(c.remote_ok)
    setTitleOptions([...new Set([...c.search_titles, ...c.titles])])
  }

  useEffect(() => { fetchCriteria().then(applyCriteria).catch(() => {}) }, [])

  const save = async () => {
    setSaveError('')
    try {
      const updated = await updateCriteria({
        titles, locations,
        acceptable_seniority: acceptable, excluded_seniority: excluded,
        salary_floor: salary[0] * 1000, date_posted_days: days,
        remote_ok: remoteOk, yoe,
      })
      applyCriteria(updated)
      setSavedToast(true)
      window.clearTimeout(toastTimer.current)
      toastTimer.current = window.setTimeout(() => setSavedToast(false), 2400)
    } catch (e) {
      setSaveError(String((e as Error).message))
    }
  }

  const reset = () => {
    setTitles(DEFAULTS.titles); setLocations(DEFAULTS.locations)
    setAcceptable(DEFAULTS.acceptable); setExcluded(DEFAULTS.excluded)
    setYoe(DEFAULTS.yoe); setSalary(DEFAULTS.salary)
    setDays(DEFAULTS.days); setRemoteOk(DEFAULTS.remoteOk)
  }

  const pillStyle = (active: boolean): React.CSSProperties => ({
    fontSize: 12.5, fontWeight: 600, padding: '7px 14px', borderRadius: 20,
    cursor: 'pointer',
    border: `1px solid ${active ? 'var(--clay)' : 'var(--border-2)'}`,
    background: active ? 'var(--clay)' : 'var(--chip)',
    color: active ? 'var(--on-clay)' : 'var(--text-2)',
  })

  return (
    <div className="card" style={{ padding: '24px 26px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div className="serif" style={{ fontSize: 16, fontWeight: 600 }}>Job criteria</div>
        <span style={{
          fontFamily: 'ui-monospace,Menlo,monospace', fontSize: 11,
          color: 'var(--text-5)', background: 'var(--chip)',
          padding: '3px 9px', borderRadius: 7,
        }}>job_criteria.yaml</span>
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-4)', margin: '4px 0 18px' }}>
        The baseline every posting is filtered against. Changes here re-scope your
        watchlist searches and the standing digest.
      </div>

      <div style={{ marginBottom: 16 }}>
        <div style={FIELD_LABEL}>Target titles</div>
        <MultiSelect placeholder="Search job titles…" options={titleOptions}
          selected={titles} onChange={setTitles}
          chipBg="var(--accent-soft)" chipColor="var(--clay-text)" />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px 24px', marginBottom: 16 }}>
        <RangeSlider label="Years of experience" min={0} max={15} step={1}
          value={yoe} onChange={setYoe} unit="yrs" />
        <div>
          <RangeSlider label="Salary range" min={60} max={400} step={5}
            value={salary} onChange={setSalary} unit="k" />
          <div style={{ fontSize: 11.5, color: 'var(--text-4)', marginTop: 6 }}>
            Written as <b style={{ color: 'var(--text-2)' }}>salary_floor: ${salary[0]}k</b>
            {' '}· disclosed pay below this is dropped.
          </div>
        </div>
      </div>

      <div style={{ marginBottom: 18 }}>
        <div style={FIELD_LABEL}>Posted within</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          {POSTED_WITHIN.map((n) => (
            <button key={n} style={pillStyle(days === n)} onClick={() => setDays(n)}>
              {n} days
            </button>
          ))}
          <span style={{ fontSize: 11.5, color: 'var(--text-4)', marginLeft: 6 }}>
            maps to <b style={{ color: 'var(--text-2)' }}>date_posted_days: {days}</b>
          </span>
        </div>
      </div>

      <div style={{ height: 1, background: 'var(--divider)', margin: '4px 0 20px' }} />
      <div style={{
        fontSize: 11.5, fontWeight: 700, letterSpacing: '.05em',
        textTransform: 'uppercase', color: 'var(--text-4)', marginBottom: 14,
      }}>Additional parameters</div>

      <div style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div style={FIELD_LABEL}>Locations allowed</div>
          <span style={{
            display: 'flex', alignItems: 'center', gap: 8, marginBottom: 7,
            fontSize: 12.5, fontWeight: 600, color: 'var(--text-2)',
          }}>
            Remote OK
            <Toggle on={remoteOk} onChange={setRemoteOk} />
          </span>
        </div>
        <MultiSelect placeholder="Search locations…"
          options={['Los Angeles', 'San Francisco', 'New York', 'Remote', 'Austin', 'Seattle', 'Boston']}
          selected={locations} onChange={setLocations}
          chipBg="var(--sage-soft)" chipColor="var(--sage-text)" />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px 24px', marginBottom: 22 }}>
        <div>
          <div style={FIELD_LABEL}>Acceptable seniority</div>
          <MultiSelect placeholder="Add seniority…" options={ACCEPTABLE_SENIORITY}
            selected={acceptable} onChange={setAcceptable} allowFree={false}
            chipBg="var(--chip)" chipColor="var(--text-2)" />
        </div>
        <div>
          <div style={FIELD_LABEL}>Excluded seniority</div>
          <MultiSelect placeholder="Add exclusion…" options={EXCLUDED_SENIORITY}
            selected={excluded} onChange={setExcluded}
            chipBg="var(--accent-soft)" chipColor="var(--clay-text)" />
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <button className="btn-primary" onClick={save}>Save to job_criteria.yaml</button>
        <button className="btn-soft" onClick={reset}>Reset to defaults</button>
        {savedToast && (
          <span style={{ fontSize: 12.5, color: 'var(--sage-text)', fontWeight: 600 }}>
            ✓ Saved — searches re-scoped
          </span>
        )}
        {saveError && (
          <span style={{ fontSize: 12.5, color: 'var(--amber-text)' }}>{saveError}</span>
        )}
      </div>
    </div>
  )
}
