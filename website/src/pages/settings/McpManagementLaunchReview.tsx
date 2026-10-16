/**
 * A row's share controls on the MCP page: its one labelled button and its
 * Details. `McpManagement` mounts them per row.
 *
 * The page shows no launch command anywhere. A press reads the launch at that
 * moment and the write carries its `expected_launch`, so the approval is pinned
 * to the exact launch the gateway resolved whether or not it was ever on screen;
 * a later change to it still pauses sharing until the person presses again.
 */
import { useState } from 'react'
import { ChevronRight, Share2 } from 'lucide-react'
import type { McpManagedServer } from '../../api/client'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { type LaunchRefusal, launchIsApprovable } from './mcpManagementModel'

/**
 * Which agents declare a server, collapsed by default. It lists names only: the
 * command the server runs is not shown on this page.
 */
export function ServerDetails({ server }: { server: McpManagedServer }) {
  const [open, setOpen] = useState(false)
  if (server.agents.length === 0) return null
  return (
    <details
      className="group mt-1"
      open={open}
      onToggle={e => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-[12px] text-[var(--muted)] hover:text-[var(--text)] [&::-webkit-details-marker]:hidden">
        <ChevronRight size={12} className="transition-transform group-open:rotate-90" aria-hidden="true" />
        {i18nT('pages.mcpManagement.details')}
      </summary>
      <p className="mt-1.5 break-words pl-4 text-[12px] leading-relaxed text-[var(--muted)] [overflow-wrap:anywhere]">
        {i18nT('pages.mcpManagement.details_used_by', { agents: server.agents.join(', ') })}
      </p>
    </details>
  )
}

/** The id of a row's server-name element, so a button can name its server. */
export function serverNameId(name: string): string {
  return `mcp-row-name-${name}`
}

/** Every share action is a real, labelled button, never a bare switch or link. */
export const SHARE_BUTTON_CLASS =
  'shrink-0 px-3 py-1.5 font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]'

/**
 * The one action a row offers.
 *
 * "Share this server" is one click: the page reads the launch and, if it can be
 * approved, writes the share carrying its `expected_launch` (see `McpManagement`).
 * A row whose launch was refused carries the refused launch's own
 * `expected_launch`, so "Use new command" pins exactly that launch; a refused
 * launch with no approvable identity offers no button and says why. "Stop
 * sharing" stays reachable on a refused row: a launch the person cannot or will
 * not confirm must still be turnable off.
 */
export function ShareAction({
  server,
  refusal,
  busy,
  reading,
  supported,
  sharingOn,
  describedBy,
  onShare,
  onConfirm,
  onStop,
}: {
  server: McpManagedServer
  /** Set when the gateway refused this shared server's current launch. */
  refusal?: LaunchRefusal
  busy: boolean
  /** True while this row's press is reading the launch it will pin. */
  reading: boolean
  supported: boolean
  /** Page-wide sharing. While it is off a press only marks the server, so the
   *  label says that instead of claiming the server is shared. */
  sharingOn: boolean
  describedBy?: string
  onShare: () => void
  onConfirm: (expectedLaunch: string) => void
  onStop: () => void
}) {
  if (!server.can_stub) return null
  const shareLabelKey = sharingOn ? 'pages.mcpManagement.share_button' : 'pages.mcpManagement.share_button_off'
  // The accessible name is the visible label followed by the row's server
  // name, read from the page itself, so a voice command for the label on
  // screen matches it.
  const labelId = `mcp-share-label-${server.name}`
  const labelledBy = `${labelId} ${serverNameId(server.name)}`
  // "Stop sharing" only where a shared copy is actually running; a marked or
  // paused row runs its own copy per chat, so its button says it clears the mark.
  const live = sharingOn && !refusal && server.pooling_blocked_by_env !== true
  // A refused launch with no approvable identity can only be taken off the list.
  const stuck = !!refusal && !launchIsApprovable(refusal)
  const stopButton = (
    <Btn
      type="button"
      className={SHARE_BUTTON_CLASS}
      disabled={busy}
      aria-label={i18nT(
        live
          ? 'pages.mcpManagement.unshare_button_aria'
          : stuck
            ? 'pages.mcpManagement.remove_mark_button_aria'
            : 'pages.mcpManagement.unmark_button_aria',
        { name: server.name },
      )}
      aria-describedby={describedBy}
      onClick={onStop}
    >
      {i18nT(
        live
          ? 'pages.mcpManagement.unshare_button'
          : stuck
            ? 'pages.mcpManagement.remove_mark_button'
            : 'pages.mcpManagement.unmark_button',
      )}
    </Btn>
  )
  if (server.stub && refusal) {
    const approvable = launchIsApprovable(refusal)
    // A server that reached the list without a press was never approved, so
    // its first approval is a share, not a "new" command.
    const firstApproval = refusal.reason === 'added_outside_dashboard'
    return (
      <div className="flex w-full min-w-0 flex-wrap items-center gap-2 md:w-auto md:justify-end">
        {/* A server never approved here offers its share alone: beside it, a
            stop button reads as a second, opposite choice for one row. */}
        {(!firstApproval || !approvable) && stopButton}
        {approvable ? (
          <Btn
            type="button"
            primary
            className={SHARE_BUTTON_CLASS}
            disabled={busy || !supported}
            aria-labelledby={labelledBy}
            aria-describedby={describedBy}
            onClick={() => onConfirm(refusal.expected_launch as string)}
          >
            <Share2 size={14} aria-hidden="true" />
            <span id={labelId}>
              {i18nT(
                firstApproval
                  ? shareLabelKey
                  : sharingOn
                    ? 'pages.mcpManagement.new_command_button'
                    : 'pages.mcpManagement.new_command_button_off',
              )}
            </span>
          </Btn>
        ) : null}
        {/* No approvable identity means no button; the row's hint says why. */}
      </div>
    )
  }
  // Wrapped like the share button, so on a narrow screen it keeps its own width
  // instead of stretching across the stacked row.
  if (server.stub) {
    return <div className="flex w-full min-w-0 items-start md:w-auto md:justify-end">{stopButton}</div>
  }
  return (
    <div className="flex w-full min-w-0 flex-col items-start gap-1 md:w-auto md:items-end">
      <Btn
        type="button"
        primary
        className={SHARE_BUTTON_CLASS}
        disabled={busy || !supported}
        aria-labelledby={labelledBy}
        aria-describedby={describedBy}
        onClick={onShare}
      >
        <Share2 size={14} aria-hidden="true" />
        <span id={labelId}>{i18nT(shareLabelKey)}</span>
      </Btn>
      {reading && (
        <span role="status" className="text-[12px] text-[var(--muted)]">
          {i18nT('pages.mcpManagement.share_checking')}
        </span>
      )}
    </div>
  )
}
