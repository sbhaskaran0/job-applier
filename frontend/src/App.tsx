import { useCallback, useEffect, useState } from 'react'
import {
  fetchApplications, fetchConnections, fetchPostings, fetchProfile,
  fetchStatus, fetchWatchlist,
} from './api'
import { useAgentChat } from './chat'
import type {
  ApplicationRecord, Connection, Page, Posting, Profile, Status,
  WatchlistCompany,
} from './types'
import Sidebar from './components/Sidebar'
import ChatPage from './components/ChatPage'
import PostingsPage from './components/PostingsPage'
import ApplicationsPage from './components/ApplicationsPage'
import ProfilePage from './components/ProfilePage'
import ConnectionsPage from './components/ConnectionsPage'
import ApplyModal from './components/ApplyModal'
import Onboarding from './components/Onboarding'

export default function App() {
  const [page, setPage] = useState<Page>('chat')
  const [theme, setTheme] = useState<'light' | 'dark'>(
    () => (localStorage.getItem('applyer-theme') as 'light' | 'dark') ?? 'dark',
  )
  const [autonomous, setAutonomous] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [modalOpen, setModalOpen] = useState(false)
  const [onboardingOpen, setOnboardingOpen] = useState(false)

  const [status, setStatus] = useState<Status | null>(null)
  const [postings, setPostings] = useState<Posting[]>([])
  const [postingsNote, setPostingsNote] = useState<string | undefined>()
  const [applications, setApplications] = useState<ApplicationRecord[]>([])
  const [profile, setProfile] = useState<Profile | null>(null)
  const [watchlist, setWatchlist] = useState<WatchlistCompany[]>([])
  const [connections, setConnections] = useState<Connection[]>([])
  const [connectionsNote, setConnectionsNote] = useState('')

  const chat = useAgentChat()

  const reload = useCallback(() => {
    fetchStatus().then(setStatus).catch(() => setStatus(null))
    fetchPostings()
      .then((r) => { setPostings(r.postings); setPostingsNote(r.note) })
      .catch((e) => setPostingsNote(String(e)))
    fetchApplications().then((r) => setApplications(r.applications)).catch(() => {})
    fetchProfile().then(setProfile).catch(() => {})
    fetchWatchlist().then((r) => setWatchlist(r.companies)).catch(() => {})
    fetchConnections()
      .then((r) => { setConnections(r.connections); setConnectionsNote(r.note) })
      .catch(() => {})
  }, [])

  useEffect(() => { reload() }, [reload])

  // A finished agent turn may have logged applications / added postings —
  // refresh the data surfaces when the agent stops typing.
  useEffect(() => {
    if (!chat.typing) reload()
  }, [chat.typing, reload])

  useEffect(() => {
    localStorage.setItem('applyer-theme', theme)
    document.documentElement.dataset.theme = theme
  }, [theme])

  const selectedPostings = postings.filter((p) => selected.has(p.url))

  const launchApply = (auto: boolean) => {
    const urls = selectedPostings.map((p) => p.url)
    if (!urls.length) return
    setModalOpen(false)
    setSelected(new Set())
    setPage('chat')
    chat.send(`/apply-batch ${auto ? 'autonomous ' : ''}${urls.join(' ')}`)
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
        newCount={postings.filter((p) => p.is_new).length}
        pendingConnections={connections.filter((c) => !c.connected).length}
        theme={theme} setTheme={setTheme}
        openOnboarding={() => setOnboardingOpen(true)}
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
          onClose={() => setModalOpen(false)} onConfirm={() => launchApply(autonomous)}
        />
      )}
      {onboardingOpen && profile && (
        <Onboarding
          profile={profile} connections={connections}
          onClose={() => { setOnboardingOpen(false); reload() }}
          onProfileSaved={setProfile}
        />
      )}
    </div>
  )
}
