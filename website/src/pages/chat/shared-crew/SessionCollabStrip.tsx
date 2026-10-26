/** The open session's collaboration state, above the composer: who else is
 *  here and this session's shared decisions. Accepted work is not listed here:
 *  a send the host holds for capacity shows on its own message bubble
 *  (`QueuedSendLabel`). Each part reads only when the Crew reports the
 *  capability behind it, and the whole strip renders nothing when there is
 *  nothing to say, so a single-operator Crew's chat is unchanged. It also
 *  reports this tab's presence for the session. */
import { useEffect } from 'react'
import { capabilityUsable } from '../../../api/sharedCrewUi'
import ErrorNotice from '../../../components/ErrorNotice'
import {
  useSessionDecisions,
  useSessionPresence,
  useSessionSendState,
  useSharedCrewCapabilities,
  useSharedCrewViewer,
} from '../../../hooks/sharedCrew/useSharedCrew'
import { usePresenceReporter } from '../../../hooks/sharedCrew/usePresenceReporter'
import { i18nT } from '../../../i18n/t'
import { DecisionAttributionList } from './DecisionAttributionList'
import { PresenceChips } from './PresenceChips'

/** A composer `input` listener target: the strip tells the reporter about
 *  keystrokes without ever reading what was typed. */
export interface TypingSignal {
  subscribe(listener: () => void): () => void
  /** Fires when the composer's draft is sent, ending this tab's typing. */
  subscribeSent?(listener: () => void): () => void
}

export function SessionCollabStrip({ sessionKey, typing }: { sessionKey: string | null | undefined; typing?: TypingSignal }) {
  const { capabilities } = useSharedCrewCapabilities()
  const presenceOn = capabilityUsable(capabilities.presence)
  const attributionOn = capabilityUsable(capabilities.attribution)
  const viewer = useSharedCrewViewer(presenceOn || attributionOn)
  const presence = useSessionPresence(sessionKey, presenceOn)
  const decisions = useSessionDecisions(sessionKey, attributionOn)
  // The revision this tab's next send is based on (first submit wins). Read for
  // every open session: the host settles every send against it, whichever
  // capabilities the Crew reports, and app or automation sends count too.
  useSessionSendState(sessionKey)
  const { noteTyping, noteSent } = usePresenceReporter(sessionKey, presenceOn)
  useEffect(() => typing?.subscribe(noteTyping), [typing, noteTyping])
  useEffect(() => typing?.subscribeSent?.(noteSent), [typing, noteSent])

  const entries = presence.data?.entries ?? []
  const decisionItems = decisions.data?.items ?? []
  const failed = presence.isError ? presence.error : decisions.isError ? decisions.error : null
  if (sessionKey && failed) {
    const message = failed instanceof Error ? failed.message : i18nT('sharedCrew.session_state_failed')
    return <ErrorNotice message={message} variant="inline" askAgent testId="session-collab-error" />
  }
  if (!sessionKey || (entries.length === 0 && decisionItems.length === 0)) return null
  return (
    <section
      className="w-full px-3 py-1 flex flex-col gap-1 min-w-0"
      aria-label={i18nT('sharedCrew.collab_region')}
      data-testid="session-collab-strip"
    >
      <PresenceChips entries={entries} viewer={viewer.data} />
      <DecisionAttributionList items={decisionItems} />
    </section>
  )
}
