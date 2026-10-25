/** Settings > People > Who can open this Crew.
 *
 *  Tailscale is the only access authority, and the set of people who get in is
 *  computed, never typed: the owner, people who accepted a Crew invite, and
 *  people the owner approves here. Someone else on the tailnet who opens the
 *  Crew's link is refused with a "waiting for the owner" page and shows up in
 *  Waiting (name and picture from Tailscale, first seen). The owner approves or
 *  dismisses them. Removing an approved person refuses their next click, closes
 *  their tabs, cancels their queued work and stops a running turn at once.
 *
 *  The first invite or approve, while nobody else has access, asks once per Crew
 *  (`useFirstShareGate`): teammates will see every session and Crew memory, with
 *  real counts, and a laptop host loses them when it sleeps.
 *
 *  The card is owner-only: the read answers 403 to anyone else and the card
 *  renders nothing. */
import { useCallback } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, UserMinus, X } from 'lucide-react'
import { api } from '../../api/client'
import { inviteErrorCode } from '../../api/sharedCrewInvites'
import { initials, safeAvatarUrl, type AccessView, type ApprovedPerson, type FirstShare, type WaitingPerson } from '../../api/sharedCrewAccess'
import { useConfirm } from '../../components/ConfirmDialog'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { Badge, Btn } from '../../components/ui'
import { sharedCrewKeys } from '../../hooks/sharedCrew/sharedCrewSocket'
import { fmtDateTime, fmtList, fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

export const sharedCrewAccessKey = [...sharedCrewKeys.all, 'access'] as const

export function useSharedCrewAccess() {
  return useQuery({
    queryKey: sharedCrewAccessKey,
    queryFn: () => api.sharedCrewAccess(),
    retry: false,
    // Someone may be waiting right now; re-read while the page is open.
    refetchInterval: 30_000,
  })
}

function accessErrorText(code: string): string {
  switch (code) {
    case 'first_share_ack_required': return i18nT('sharedCrew.waitlist_err_ack')
    case 'not_waiting': return i18nT('sharedCrew.waitlist_err_gone')
    case 'owner_only': return i18nT('sharedCrew.invite_err_owner')
    default: return i18nT('sharedCrew.waitlist_err_generic')
  }
}

/** What teammates will see, as the warning's body. */
function FirstShareBody({ share }: { share: FirstShare }) {
  const counted = share.sessions !== null && share.memory_items !== null
  const laptop = share.host.laptop
  return (
    <div className="flex flex-col gap-2" data-testid="first-share-body">
      <p className="m-0">
        {counted
          ? i18nT('sharedCrew.first_share_body', { sessions: fmtNumber(share.sessions ?? 0), memory: fmtNumber(share.memory_items ?? 0) })
          : i18nT('sharedCrew.first_share_body_uncounted')}
      </p>
      {laptop && (
        <p className="m-0 text-muted" data-testid="first-share-laptop">
          {share.host.state === 'available' ? i18nT('sharedCrew.first_share_laptop') : i18nT('sharedCrew.first_share_laptop_no_keep_awake')}
        </p>
      )}
    </div>
  )
}

/** Ask once per Crew before the first share. `guard(action)` runs *action* when
 *  no warning is due, or after the owner confirms it (and records that). */
export function useFirstShareGate() {
  const qc = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const guard = useCallback(async (action: () => void) => {
    // A fresh read at the moment of sharing: the warning is due only while
    // nobody else has access. A failed read leaves the decision to the server,
    // which refuses an unacknowledged first share on its own.
    let view: AccessView | null = null
    try {
      view = await qc.fetchQuery({ queryKey: sharedCrewAccessKey, queryFn: () => api.sharedCrewAccess(), staleTime: 0 })
    } catch {
      view = null
    }
    const share = view?.first_share
    if (!share?.needs_ack) { action(); return }
    const ok = await confirm({
      title: i18nT('sharedCrew.first_share_title'),
      body: <FirstShareBody share={share} />,
      confirmLabel: i18nT('sharedCrew.first_share_confirm'),
      danger: false,
      primary: true,
    })
    if (!ok) return
    await api.sharedCrewAckFirstShare()
    await qc.invalidateQueries({ queryKey: sharedCrewAccessKey })
    action()
  }, [confirm, qc])
  return { guard, firstShareDialog: confirmDialog }
}

function Avatar({ name, url }: { name: string; url: string }) {
  const src = safeAvatarUrl(url)
  return src ? (
    <img src={src} alt="" referrerPolicy="no-referrer" className="w-8 h-8 rounded-full object-cover shrink-0" />
  ) : (
    <span className="inline-flex items-center justify-center w-8 h-8 rounded-full bg-bg-hover text-text text-[12px] font-semibold shrink-0" aria-hidden>
      {initials(name)}
    </span>
  )
}

function WaitingRow({ person, guard }: { person: WaitingPerson; guard: (action: () => void) => Promise<void> }) {
  const qc = useQueryClient()
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: sharedCrewAccessKey })
    void qc.invalidateQueries({ queryKey: sharedCrewKeys.people })
  }
  const approve = useMutation({ mutationFn: () => api.sharedCrewApprove(person.user_id), onSuccess: refresh })
  const dismiss = useMutation({ mutationFn: () => api.sharedCrewDismiss(person.user_id), onSuccess: refresh })
  const name = person.display_name || person.login
  const err = approve.isError ? inviteErrorCode(approve.error) || 'error' : dismiss.isError ? inviteErrorCode(dismiss.error) || 'error' : ''
  return (
    <li className="flex flex-wrap items-center gap-3 py-2 min-w-0" data-testid={`waiting-${person.user_id}`}>
      <Avatar name={name} url={person.avatar_url} />
      <div className="flex flex-col gap-0.5 min-w-0 flex-1">
        <span className="text-sm font-medium text-text-strong truncate max-w-full" title={name}>{name}</span>
        {name !== person.login && <span className="text-[12px] text-muted truncate" title={person.login}>{person.login}</span>}
        <span className="text-[12px] text-muted">{i18nT('sharedCrew.waitlist_first_seen', { when: fmtDateTime(person.first_seen * 1000) })}</span>
      </div>
      <div className="flex flex-wrap gap-2">
        <Btn type="button" primary onClick={() => { void guard(() => approve.mutate()) }} disabled={approve.isPending} data-testid="waiting-approve">
          <Check size={14} className="lucide-inline" aria-hidden /> {i18nT('sharedCrew.waitlist_approve')}
        </Btn>
        <Btn type="button" onClick={() => dismiss.mutate()} disabled={dismiss.isPending} data-testid="waiting-dismiss">
          <X size={14} className="lucide-inline" aria-hidden /> {i18nT('sharedCrew.waitlist_dismiss')}
        </Btn>
      </div>
      {/* No draft sits beside an approve or dismiss, so the hand-off loses nothing. */}
      {err && <ErrorNotice variant="inline" className="w-full" message={accessErrorText(err)} askAgent testId="waiting-error" />}
    </li>
  )
}

function ApprovedRow({ person }: { person: ApprovedPerson }) {
  const qc = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const remove = useMutation({
    mutationFn: () => api.sharedCrewRemoveApproved(person.user_id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: sharedCrewAccessKey })
      void qc.invalidateQueries({ queryKey: sharedCrewKeys.people })
    },
  })
  const name = person.display_name || person.login
  const onRemove = async () => {
    const ok = await confirm({
      title: i18nT('sharedCrew.waitlist_remove_title', { name }),
      body: i18nT('sharedCrew.waitlist_remove_body'),
      confirmLabel: i18nT('sharedCrew.waitlist_remove_confirm'),
    })
    if (ok) remove.mutate()
  }
  return (
    <li className="flex flex-wrap items-center gap-3 py-2 min-w-0" data-testid={`approved-${person.user_id}`}>
      <Avatar name={name} url={person.avatar_url} />
      <div className="flex flex-col gap-0.5 min-w-0 flex-1">
        <div className="flex items-center gap-2 min-w-0 flex-wrap">
          <span className="text-sm font-medium text-text-strong truncate max-w-full" title={name}>{name}</span>
          <Badge variant="ok">{i18nT('sharedCrew.waitlist_approved_badge')}</Badge>
        </div>
        {name !== person.login && <span className="text-[12px] text-muted truncate" title={person.login}>{person.login}</span>}
        <span className="text-[12px] text-muted">{i18nT('sharedCrew.waitlist_approved_at', { when: fmtDateTime(person.approved_at * 1000) })}</span>
      </div>
      <Btn type="button" danger onClick={() => { void onRemove() }} disabled={remove.isPending} data-testid="approved-remove">
        <UserMinus size={14} className="lucide-inline" aria-hidden /> {i18nT('sharedCrew.waitlist_remove')}
      </Btn>
      {/* No draft sits beside a removal, so the hand-off loses nothing. */}
      {remove.isError && <ErrorNotice variant="inline" className="w-full" message={accessErrorText(inviteErrorCode(remove.error))} askAgent testId="approved-remove-error" />}
      {confirmDialog}
    </li>
  )
}

/** The whole card. Hidden for anyone but the owner, or a gateway without the routes (404). */
export function TailscaleAccess() {
  const access = useSharedCrewAccess()
  const { guard, firstShareDialog } = useFirstShareGate()
  if (access.isLoading) return null
  if (access.isError) {
    // A non-owner is refused by design: the card is the owner's alone.
    const code = inviteErrorCode(access.error)
    if (code === 'owner_only') return null
    return (
      <SettingsSection title={i18nT('sharedCrew.waitlist_title')}>
        <ErrorNotice message={accessErrorText(code)} askAgent testId="access-load-error" />
      </SettingsSection>
    )
  }
  // A gateway without the routes (404): nothing to show here.
  if (!access.data) return null
  const view = access.data
  return (
    <SettingsSection title={i18nT('sharedCrew.waitlist_title')}>
      <SettingsCard>
        <div className="flex flex-col gap-3 min-w-0" data-testid="tailscale-access">
          <p className="text-[13px] text-muted">{i18nT('sharedCrew.waitlist_body')}</p>
          {!view.readable && (
            <p role="alert" className="text-[12px] text-danger" data-testid="access-unreadable">{i18nT('sharedCrew.waitlist_unreadable')}</p>
          )}
          <div className="flex flex-col gap-1 min-w-0">
            <h4 className="text-[13px] font-semibold text-text-strong m-0">{i18nT('sharedCrew.waitlist_waiting_title')}</h4>
            {view.waiting.length === 0 ? (
              <p className="text-[13px] text-muted" data-testid="waiting-empty">{i18nT('sharedCrew.waitlist_waiting_empty')}</p>
            ) : (
              <ul className="flex flex-col divide-y divide-border" aria-label={i18nT('sharedCrew.waitlist_waiting_title')} data-testid="waiting-list">
                {view.waiting.map(p => <WaitingRow key={p.user_id} person={p} guard={guard} />)}
              </ul>
            )}
          </div>
          {view.approved.length > 0 && (
            <div className="flex flex-col gap-1 min-w-0 pt-2 border-t border-border">
              <h4 className="text-[13px] font-semibold text-text-strong m-0">{i18nT('sharedCrew.waitlist_approved_title')}</h4>
              <ul className="flex flex-col divide-y divide-border" aria-label={i18nT('sharedCrew.waitlist_approved_title')} data-testid="approved-list">
                {view.approved.map(p => <ApprovedRow key={p.user_id} person={p} />)}
              </ul>
            </div>
          )}
          {view.seeded.length > 0 && (
            <p className="text-[12px] text-muted break-words" data-testid="access-seeded">{i18nT('sharedCrew.waitlist_seeded', { entries: fmtList(view.seeded) })}</p>
          )}
        </div>
        {firstShareDialog}
      </SettingsCard>
    </SettingsSection>
  )
}
