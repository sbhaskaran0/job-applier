import { useCallback, useEffect, useRef, useState } from 'react'
import {
  activateProfile, createProfile, fetchApplications, fetchConnections,
  fetchPostings, fetchProfile, fetchProfiles, fetchStatus, fetchWatchlist,
} from './api'
import { useAgentChat } from './chat'
import type {
  ApplicationRecord, Connection, Page, Posting, Profile, ProfileSummary,
  Status, WatchlistCompany,
} from './types'
import Sidebar from './components/Sidebar'
import ChatPage from './components/ChatPage'
import PostingsPage from './components/PostingsPage'
import ApplicationsPage from './components/ApplicationsPage'
import ProfilePage from './components/ProfilePage'
import ConnectionsPage from './components/ConnectionsPage'
import ApplyModal from './components/ApplyModal'
import BugReportModal from './components/BugReportModal'
import Onboarding from './components/Onboarding'
import LaunchScreen from './components/LaunchScreen'
import { NewProfileModal, SwitchConfirmModal } from './components/ProfileModals'

/* A profile with no name or email yet is "incomplete" — the wizard auto-opens
   on its first load (dismissible; reopens next launch until the basics exist). */
const isIncomplete = (p: Profile) =>
  !(p.facts.full_name ?? '').trim() || !(p.facts.email ?? '').trim()

const PAGES: Page[] = ['chat', 'postings', 'applications', 'profile', 'connections']

/* Navigation is state-switched; the hash is only read once at load so a
   surface can be deep-linked (e.g. localhost:8765/#profile) for QA. */
const initialPage = (): Page => {
  const h = window.location.hash.replace('#', '') as Page
  return PAGES.includes(h) ? h : 'chat'
}

export default function App() {
  const [page, setPage] = useState<Page>(initialPage)
  const [theme, setTheme] = useState<'light' | 'dark'>(
    () => (localStorage.getItem('applyer-theme') as 'light' | 'dark') ?? 'dark',
  )
  const [autonomous, setAutonomous] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [modalOpen, setModalOpen] = useState(false)
  const [onboardingOpen, setOnboardingOpen] = useState(
    // ?wizard=N opens setup at step N on load (design QA hook)
    () => new URLSearchParams(window.location.search).has('wizard'),
  )

  const [profiles, setProfiles] = useState<ProfileSummary[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [profilesLoaded, setProfilesLoaded] = useState(false)
  const [switchTarget, setSwitchTarget] = useState<ProfileSummary | null>(null)
  const [newProfileOpen, setNewProfileOpen] = useState(false)
  const [bugModalOpen, setBugModalOpen] = useState(false)
  const [toast, setToast] = useState('')
  const toastTimer = useRef<number | undefined>(undefined)
  const autoOpenedFor = useRef<Set<string>>(new Set())

  const [status, setStatus] = useState<Status | null>(null)
  const [postings, setPostings] = useState<Posting[]>([])
  const [postingsNote, setPostingsNote] = useState<string | undefined>()
  const [hiddenByCriteria, setHiddenByCriteria] = useState<number | undefined>()
  const [applications, setApplications] = useState<ApplicationRecord[]>([])
  const [profile, setProfile] = useState<Profile | null>(null)
  const [watchlist, setWatchlist] = useState<WatchlistCompany[]>([])
  const [connections, setConnections] = useState<Connection[]>([])
  const [connectionsNote, setConnectionsNote] = useState('')
  const [gmailAccount, setGmailAccount] = useState<string | null>(null)

  const chat = useAgentChat()

  const showToast = useCallback((text: string) => {
    setToast(text)
    window.clearTimeout(toastTimer.current)
    toastTimer.current = window.setTimeout(() => setToast(''), 3200)
  }, [])

  const reload = useCallback(() => {
    fetchStatus().then(setStatus).catch(() => setStatus(null))
    fetchPostings()
      .then((r) => {
        setPostings(r.postings)
        setPostingsNote(r.note)
        setHiddenByCriteria(r.hidden_by_criteria)
      })
      .catch((e) => setPostingsNote(String(e)))
    fetchApplications().then((r) => setApplications(r.applications)).catch(() => {})
    fetchProfile().then(setProfile).catch(() => {})
    fetchWatchlist().then((r) => setWatchlist(r.companies)).catch(() => {})
    fetchConnections()
      .then((r) => {
        setConnections(r.connections)
        setConnectionsNote(r.note)
        setGmailAccount(r.gmail_account)
      })
      .catch(() => {})
  }, [])

  const loadProfiles = useCallback(() => fetchProfiles()
    .then((r) => {
      setProfiles(r.profiles)
      setActiveId(r.active_id)
      return r
    })
    .finally(() => setProfilesLoaded(true)), [])

  useEffect(() => { loadProfiles().catch(() => {}) }, [loadProfiles])
  useEffect(() => { if (activeId) reload() }, [activeId, reload])

  // A finished agent turn may have logged applications / added postings —
  // refresh the data surfaces when the agent stops typing.
  useEffect(() => {
    if (!chat.typing) reload()
  }, [chat.typing, reload])

  useEffect(() => {
    localStorage.setItem('applyer-theme', theme)
    document.documentElement.dataset.theme = theme
  }, [theme])

  // Wizard auto-opens on first load of an incomplete profile (once per
  // profile per app session — dismissing it sticks until the next launch).
  useEffect(() => {
    if (!profile || !activeId) return
    if (isIncomplete(profile) && !autoOpenedFor.current.has(activeId)) {
      autoOpenedFor.current.add(activeId)
      setOnboardingOpen(true)
    }
  }, [profile, activeId])

  const activate = async (p: ProfileSummary) => {
    try {
      const r = await activateProfile(p.id)
      setProfiles(r.profiles)
      setActiveId(r.active_id)
      setSwitchTarget(null)
      setSelected(new Set())
      showToast(`Now applying as ${p.name}`)
    } catch (e) {
      setSwitchTarget(null)
      showToast(String((e as Error).message))
    }
  }

  const onCreateProfile = async (name: string) => {
    const r = await createProfile(name)
    setProfiles(r.profiles)
    setActiveId(r.active_id)
    setNewProfileOpen(false)
    showToast(`Now applying as ${name}`)
    // brand-new profile → straight into setup
    setOnboardingOpen(true)
  }

  const selectedPostings = postings.filter((p) => selected.has(p.url))

  const launchApply = (auto: boolean, tailorUrls: string[] = []) => {
    const urls = selectedPostings.map((p) => p.url)
    if (!urls.length) return
    setModalOpen(false)
    setSelected(new Set())
    setPage('chat')
    const batch = `/apply-batch ${auto ? 'autonomous ' : ''}${urls.join(' ')}`
    const tailor = tailorUrls.filter((u) => urls.includes(u))
    if (!tailor.length) {
      chat.send(batch)
      return
    }
    // Tailor the ticked roles first (each pauses for cover-letter approval), then
    // batch-apply the whole selection — apply-batch auto-picks up the tailored
    // resume + cover letter for those roles via get_job_artifacts.
    chat.send(
      'Before applying, tailor a bespoke resume + cover letter for these roles — '
      + 'run /tailor-application once for each (pause for my approval on each cover '
      + `letter):\n${tailor.join('\n')}\n\nThen apply to the full selection: ${batch}`,
    )
  }

  // Launch screen renders INSTEAD of the shell until a profile is active.
  if (profilesLoaded && !activeId) {
    return (
      <div data-theme={theme} style={{ height: '100vh', background: 'var(--bg-app)', color: 'var(--ink)' }}>
        <LaunchScreen
          profiles={profiles}
          onPick={(p) => activate(p)}
          onNew={() => setNewProfileOpen(true)}
        />
        {newProfileOpen && (
          <NewProfileModal onCancel={() => setNewProfileOpen(false)} onCreate={onCreateProfile} />
        )}
      </div>
    )
  }

  return (
    <div
      data-theme={theme}
      style={{
        display: 'flex', height: '100vh', width: '100%', overflow: 'hidden',
        background: 'var(--bg-app)', color: 'var(--ink)', position: 'relative',
      }}
    >
      <Sidebar
        page={page} setPage={setPage} status={status} profile={profile}
        profiles={profiles}
        newCount={postings.filter((p) => p.is_new).length}
        pendingConnections={connections.filter((c) => !c.connected).length}
        theme={theme} setTheme={setTheme}
        onSwitchRequest={setSwitchTarget}
        onNewProfile={() => setNewProfileOpen(true)}
        onReportBug={() => setBugModalOpen(true)}
      />
      <main style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
        {page === 'chat' && (
          <ChatPage
            chat={chat} autonomous={autonomous} setAutonomous={setAutonomous}
            watchlist={watchlist}
            refreshWatchlist={() => fetchWatchlist().then((r) => setWatchlist(r.companies))}
          />
        )}
        {page === 'postings' && (
          <PostingsPage
            postings={postings} note={postingsNote}
            selected={selected} setSelected={setSelected}
            autonomous={autonomous} setAutonomous={setAutonomous}
            openApply={() => setModalOpen(true)}
            reload={reload}
            profileName={profile?.facts.first_name?.trim()
              || profile?.facts.full_name?.trim()?.split(/\s+/)[0]}
            hiddenByCriteria={hiddenByCriteria}
            openCriteria={() => setPage('profile')}
          />
        )}
        {page === 'applications' && <ApplicationsPage applications={applications} />}
        {page === 'profile' && (
          <ProfilePage profile={profile} openOnboarding={() => setOnboardingOpen(true)} />
        )}
        {page === 'connections' && (
          <ConnectionsPage connections={connections} note={connectionsNote} />
        )}
      </main>
      {modalOpen && (
        <ApplyModal
          jobs={selectedPostings} autonomous={autonomous}
          onClose={() => setModalOpen(false)}
          onConfirm={(tailorUrls) => launchApply(autonomous, tailorUrls)}
        />
      )}
      {onboardingOpen && profile && (
        <Onboarding
          profile={profile} connections={connections} gmailAccount={gmailAccount}
          onClose={() => { setOnboardingOpen(false); reload(); loadProfiles().catch(() => {}) }}
          onProfileSaved={setProfile}
          goToJobs={() => setPage('chat')}
        />
      )}
      {switchTarget && (
        <SwitchConfirmModal
          target={switchTarget}
          onCancel={() => setSwitchTarget(null)}
          onConfirm={() => activate(switchTarget)}
        />
      )}
      {newProfileOpen && (
        <NewProfileModal onCancel={() => setNewProfileOpen(false)} onCreate={onCreateProfile} />
      )}
      {bugModalOpen && (
        <BugReportModal
          page={page}
          onCancel={() => setBugModalOpen(false)}
          onSent={() => { setBugModalOpen(false); showToast('Thanks — bug logged for the next improvement run') }}
        />
      )}
      {toast && (
        <div style={{
          position: 'absolute', left: '50%', bottom: 26, transform: 'translateX(-50%)',
          background: '#2B2723', color: '#F6F1E8', padding: '11px 20px',
          borderRadius: 14, fontSize: 13.5, fontWeight: 500, zIndex: 90,
          boxShadow: '0 10px 30px rgba(43,39,35,0.28)', animation: 'fadeUp .25s ease',
        }}>
          {toast}
        </div>
      )}
    </div>
  )
}
