import { useCallback, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../../api/client'
import type { CrewWorker } from '../../../types'
import ErrorNotice from '../../../components/ErrorNotice'
import { fmtRelativeTime } from '../sessionOrder'
import { useAppDispatch, useAppSelector } from '../../../store'
import { closeActivityPanel, openActivityPanel, resumeFromHistory, switchSlot } from '../../../store/chatSlice'
import { useIsMobile } from '../../../hooks/useIsMobile'
import { openPanelView } from '../../../hooks/usePanelTabs'
import { CrewStatusDot } from '../../../components/crew/CrewModeMarks'
import { CREW_GROUP_LABEL_KEY, crewWorkerGroup, groupCrewWorkers } from '../../../lib/crewWorkers'
import CrewTimeline from './CrewTimeline'

/** The status labels, keyed by the wire value `GET .../crew-workers` sends. */
export const CREW_WORKER_STATUS_KEY: Record<CrewWorker['status'], string> = {
  working: 'commandCenter.worker_status_working',
  idle: 'commandCenter.worker_status_idle',
  needs_approval: 'commandCenter.worker_status_needs_approval',
  asking: 'commandCenter.worker_status_asking',
  closed: 'commandCenter.worker_status_closed',
}

/** The query key of one root's worker list, for anything that must re-read it. */
export const crewWorkersQueryKey = (root: string) => ['crew-workers', root] as const

/** One root's worker list, re-read every 5s while shown. Shared by the side panel
 *  and the worker view's breadcrumb, so both name a worker the same way. */
export function useCrewWorkers(root: string, active: boolean) {
  return useQuery({
    queryKey: crewWorkersQueryKey(root),
    queryFn: () => api.crewWorkers(root),
    enabled: active && !!root,
    refetchInterval: active ? 5000 : false,
  })
}

/** One root's workers for its session-list summary and the breadcrumb: the same
 *  query (and cache) the panel reads, re-read every 15s when the panel is not.
 *  `failed` is true while the latest read failed: the caller says so rather than
 *  showing counts from an older read, or nothing at all. */
export function useCrewWorkerSummary(root: string): {
  workers: CrewWorker[] | undefined; failed: boolean; savedTitle: string
  /** The last list a read gave, kept through a failed poll (the cache's data). */
  lastWorkers: CrewWorker[] | undefined
} {
  const query = useQuery({
    queryKey: crewWorkersQueryKey(root),
    queryFn: () => api.crewWorkers(root),
    enabled: !!root,
    refetchInterval: 15_000,
  })
  // A closed root's list is read from saved data and names the root's saved title.
  const savedTitle = query.data?.root_closed ? query.data.root_title || '' : ''
  return { workers: query.isError ? undefined : query.data?.workers, failed: query.isError, savedTitle, lastWorkers: query.data?.workers }
}

/** Whether `key` is a done worker on `root`'s worker list: the server's group,
 *  read from the same query the panel and breadcrumb share. */
export function useCrewWorkerIsDone(root: string, key: string): boolean {
  const { workers } = useCrewWorkerSummary(root)
  return !!workers?.some(w => w.key === key && w.group === 'done')
}

/** Open one of a root's sessions -- a worker, or the root itself -- in the main
 *  pane, with the Workers tab in front in its side panel so switching is one click.
 *  A plain `switchSlot`, like a tab click: the composer keeps each session's draft,
 *  so leaving one needs no guard.
 *
 *  Each session remembers its own panel open state, so on its own the switch would
 *  restore the target's (usually closed) panel. With `keepPanel` -- every desktop
 *  open -- the panel is opened on the target too, so the Workers list stays beside
 *  the worker. A phone passes false: there the panel is a sheet over the chat. */
export function openCrewSession(dispatch: ReturnType<typeof useAppDispatch>, key: string, keepPanel = false) {
  openPanelView(key, 'workers')
  dispatch(switchSlot({ key, announceOnMissing: true }))
  // `switchSlot.pending` has already made `key` the active slot and restored ITS
  // remembered panel state. Desktop keeps the panel beside the chat; a phone
  // always lands on the chat, even when the target last had its sheet open.
  dispatch(keepPanel ? openActivityPanel() : closeActivityPanel())
}

/** Reopen a CLOSED worker from its saved transcript and open it like a live one.
 *
 *  History leaves workers out, so the conductor's panel is their way back: the
 *  reopened tab keeps its root (its saved metadata names its creator), so it is
 *  listed under this root again and can be steered. Resolves false when the
 *  transcript could not be reopened. */
export async function reopenCrewWorker(
  dispatch: ReturnType<typeof useAppDispatch>, key: string, title: string, keepPanel = false,
): Promise<boolean> {
  try {
    const resumed = await dispatch(resumeFromHistory({ key: `dashboard_${key}`, title })).unwrap()
    if (!resumed.ok) return false
    openCrewSession(dispatch, resumed.key || key, keepPanel)
    return true
  } catch {
    return false
  }
}

/**
 * A Crew Mode root's workers in the side panel: cards grouped Approval needed, Working,
 * Waiting, Done, each heading with its count, what needs the person first. A
 * card opens that worker in the MAIN pane like any session; the card for the
 * worker on screen is marked current, so moving between workers is one click.
 *
 * Props:
 * - `root` -- the Crew Mode root's slot key (`crew_mode_root` on its row, or the
 *   `crew_root` a worker row names).
 * - `viewing` -- the slot the main pane shows: the root or one of its workers.
 * - `active` -- the panel is on screen; the list is re-read only while it is.
 *
 * A **List | Timeline** switch on top swaps the cards for the root's timeline
 * (`CrewTimeline`); List is the default. A phone has no switch: its panel is a
 * sheet too narrow for lanes, so it always shows the list.
 */
export default function CrewWorkerList({ root, viewing, active }: {
  root: string
  viewing: string | null
  active: boolean
}) {
  const { t } = useTranslation()
  const dispatch = useAppDispatch()
  // On a phone the panel is a sheet over the chat: opening a worker closes it, so
  // the worker fills the screen and its breadcrumb's Back is the way out.
  const isMobile = useIsMobile()
  const [reopenFailed, setReopenFailed] = useState(false)
  // Stable across renders, so the timeline's memoised lanes keep their props. A
  // closed worker (its tab is gone) is reopened from its saved transcript first.
  const open = useCallback((key: string, title = '', closed = false) => {
    if (!closed) {
      openCrewSession(dispatch, key, !isMobile)
      return
    }
    setReopenFailed(false)
    void reopenCrewWorker(dispatch, key, title, !isMobile).then(ok => { if (!ok) setReopenFailed(true) })
  }, [dispatch, isMobile])
  const [view, setView] = useState<'list' | 'timeline'>('list')
  const showTimeline = view === 'timeline' && !isMobile
  // Read only while the cards are on screen: the timeline has its own read.
  const workers = useCrewWorkers(root, active && !showTimeline)
  const groups = groupCrewWorkers(workers.data?.workers ?? [])
  return <div className="h-full flex flex-col min-w-0" data-testid="crew-worker-list">
    {!isMobile && <CrewViewSwitch view={view} onChange={setView} />}
    {showTimeline
      ? <div className="flex-1 min-h-0 overflow-y-auto"><CrewTimeline root={root} viewing={viewing} active={active} onOpen={open} /></div>
      : <div className="flex-1 min-h-0 overflow-y-auto p-3 space-y-4">
      {/* A read failure with nothing to lose: the composer's draft is saved per
          chat (utils/chatDrafts), so the hand-off may navigate. */}
      {workers.isError && <ErrorNotice message={t('commandCenter.workers_error')} askAgent />}
      {reopenFailed && <ErrorNotice message={t('commandCenter.worker_reopen_failed')} />}
      {/* The first read is in flight: say so, so a slow read is not an empty panel. */}
      {workers.isPending && <p role="status" aria-busy="true" className="text-sm text-muted" data-testid="crew-workers-loading">{t('commandCenter.workers_loading')}</p>}
      {workers.isSuccess && groups.length === 0 && <p className="text-sm text-muted">{t('commandCenter.workers_empty')}</p>}
      {groups.map(({ group, workers: rows }) => (
        <section key={group} aria-labelledby={`crew-group-${group}`} data-testid="crew-worker-group" data-group={group}>
          <h3 id={`crew-group-${group}`} className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted">
            {t(CREW_GROUP_LABEL_KEY[group])} · {rows.length}
          </h3>
          <ul className="space-y-2">
            {rows.map(w => <CrewWorkerCard key={w.key} worker={w} current={w.key === viewing} onOpen={() => open(w.key, w.title, w.status === 'closed')} />)}
          </ul>
        </section>
      ))}
    </div>}
    <p className="shrink-0 border-t border-border px-3 py-2 text-[12px] text-muted" data-testid="crew-projects-soon">{t('commandCenter.projects_coming_soon')}</p>
  </div>
}

/** The panel's List | Timeline switch: two radio-like buttons in one group. */
function CrewViewSwitch({ view, onChange }: { view: 'list' | 'timeline'; onChange: (view: 'list' | 'timeline') => void }) {
  const { t } = useTranslation()
  const option = (value: 'list' | 'timeline', label: string) => (
    <button
      type="button"
      aria-pressed={view === value}
      data-testid={`crew-view-${value}`}
      onClick={() => onChange(value)}
      className={`flex-1 rounded-md px-2 py-1 text-[12px] font-medium transition-colors ${view === value ? 'bg-bg-elevated text-text shadow-sm' : 'text-muted hover:text-text'}`}
    >{label}</button>
  )
  return <div className="shrink-0 px-3 pt-3">
    <div role="group" aria-label={t('commandCenter.workers_view_label')} className="flex gap-1 rounded-lg border border-border bg-bg-hover/40 p-0.5" data-testid="crew-view-switch">
      {option('list', t('commandCenter.workers_view_list'))}
      {option('timeline', t('commandCenter.workers_view_timeline'))}
    </div>
  </div>
}

function CrewWorkerCard({ worker, current, onOpen }: { worker: CrewWorker; current: boolean; onOpen: () => void }) {
  const { t } = useTranslation()
  const closed = worker.status === 'closed'
  const title = worker.title || t('commandCenter.worker_untitled')
  const meta = [
    worker.last_activity ? fmtRelativeTime(worker.last_activity) : '',
  ].filter(Boolean).join(' · ')
  const body = <>
    <span className="flex items-center gap-2 min-w-0">
      <span className="font-medium text-text truncate">{title}</span>
      <span className="ml-auto"><CrewStatusDot group={crewWorkerGroup(worker)} /></span>
    </span>
    {worker.summary && <span className="block text-[12px] text-text/80 truncate mt-0.5" data-testid="crew-worker-summary">{worker.summary}</span>}
    {meta && <span className="block text-[12px] text-muted truncate mt-0.5">{meta}</span>}
  </>
  const cls = `block w-full text-left rounded-lg border px-3 py-2.5 text-sm transition-colors ${current ? 'crew-card' : 'border-border bg-bg-elevated/40 hover:bg-bg-hover'}`
  // A closed worker opens too: from its saved transcript, under this root.
  return <li data-testid="crew-worker-row" data-worker={worker.key}>
    {closed
      ? <button type="button" className={`${cls} cursor-pointer min-h-11`} aria-label={t('commandCenter.worker_reopen', { title })} data-testid="crew-worker-reopen" onClick={onOpen}>{body}</button>
      : <button type="button" className={`${cls} cursor-pointer min-h-11`} aria-current={current ? 'true' : undefined} onClick={onOpen}>{body}</button>}
  </li>
}

/** The side panel's Workers tab for `slot`: its root's worker list when the slot
 *  is a Crew Mode root or one of its workers, and one line saying so otherwise. */
export function CrewWorkersTab({ slot, active }: { slot: string | null; active: boolean }) {
  const { t } = useTranslation()
  const row = useAppSelector(s => (slot ? s.dashboard.slots.find(item => item.key === slot) : undefined))
  const root = row?.crew_mode_root ? row.key : row?.crew_root || ''
  if (!root) return <p className="p-3 text-sm text-muted" data-testid="crew-workers-none">{t('commandCenter.workers_not_crew')}</p>
  return <CrewWorkerList root={root} viewing={slot} active={active} />
}
