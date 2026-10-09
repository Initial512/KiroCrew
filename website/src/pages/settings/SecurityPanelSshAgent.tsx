/**
 * Settings -> Security -> SSH agent forwarding: the owner grant that forwards the
 * operator's ssh-agent socket into new sessions. One of the sections
 * `SecurityPanel.tsx` composes.
 */
import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Copy, Check } from 'lucide-react'
import { Badge, Btn } from '../../components/ui'
import { SettingsCard } from '../../components/settings'
import {
  api, type ArmedSshAgentConsent, type SshAgentConsentStatus,
} from '../../api/client'
import { i18nT } from '../../i18n/t'
import { fmtDateTime, fmtDuration } from '../../i18n/format'
import ErrorNotice from '../../components/ErrorNotice'
import { copyToClipboard } from '../../utils/clipboard'

/* ── SSH agent forwarding consent ───────────────────────────────────────────
 *
 * A VIEW over the grant the `/api/ssh-agent/consent` endpoints own, built on the
 * flagged-file delivery card (`SecurityPanelApprovals.tsx`) and deliberately indistinguishable from it in
 * grammar: one row, one badge, one action, one armed-state block with the host
 * command. Everything that card says about authority holds here too — the owner-
 * gated handler is the only writer, the cached read is never the authority, and
 * after a write this card INVALIDATES rather than asserting the new state.
 *
 * What is different is what the grant covers. Forwarding the ssh-agent socket
 * lets any code a session runs authenticate as the owner with every key the
 * agent holds, for the whole session. That is why the risk line is always
 * visible rather than folded into a tooltip, and why the grant is read at agent
 * SPAWN: sessions already running are unaffected, so the copy says "new
 * sessions" and never implies a running one changed.
 *
 * `socket_present` is the GATEWAY process's view: when the gateway itself has no
 * `SSH_AUTH_SOCK` there is nothing to forward, and the card says so instead of
 * offering a grant that would silently forward nothing.
 */
export function SshAgentConsentCard() {
  const qc = useQueryClient()
  const { data, isLoading, isError } = useQuery<SshAgentConsentStatus>({
    queryKey: ['ssh-agent-consent'],
    queryFn: api.sshAgentConsent,
  })
  // The armed request (if any). Same nonce-free view as file delivery: the
  // browser learns only that a request is pending and which host command
  // finishes it. Polled every 3s while armed so the countdown and the flip to
  // granted both surface without a manual refresh.
  const armed = useQuery<ArmedSshAgentConsent>({
    queryKey: ['ssh-agent-consent-arm'],
    queryFn: api.sshAgentConsentArmStatus,
    refetchInterval: q => (q.state.data?.armed ? 3000 : false),
  })
  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['ssh-agent-consent'] })
    void qc.invalidateQueries({ queryKey: ['ssh-agent-consent-arm'] })
  }
  const arm = useMutation({
    mutationFn: () => api.armSshAgentConsent(),
    onSuccess: invalidate,
  })
  // One DELETE serves both Revoke (a recorded grant) and Cancel (an armed, not
  // yet approved request): both move in the fail-safe direction, so neither
  // needs the step-up, and the backend's DELETE discards whichever is held.
  const revoke = useMutation({
    mutationFn: () => api.revokeSshAgentConsent(),
    onSuccess: invalidate,
  })

  // Same rule as the delivery card: an unreadable state renders NO state, gated
  // on `isError` rather than on `data === undefined`, because react-query keeps
  // the last good `data` across a failed refetch and "Not allowed" for a grant
  // that is now live is the dangerous direction of wrong.
  const view = isError ? undefined : data
  const armedView = armed.isError ? undefined : armed.data

  const [cmdCopied, setCmdCopied] = useState(false)
  const [cmdCopyFailed, setCmdCopyFailed] = useState(false)
  useEffect(() => {
    if (!cmdCopied) return
    const id = window.setTimeout(() => setCmdCopied(false), 1500)
    return () => window.clearTimeout(id)
  }, [cmdCopied])
  // Whether a request has been armed at least once this mount, so an armed box
  // that later disappears (expiry/consume) leaves a trace instead of a silent
  // unmount that reads as a dead button.
  const [wasArmed, setWasArmed] = useState(false)
  // Whether the owner ended the armed request with Cancel (as opposed to it
  // expiring). The card knows which happened, so the trailing notice says so
  // instead of hedging "expired or was cancelled".
  const [cancelled, setCancelled] = useState(false)
  const isArmed = !!armedView?.armed
  useEffect(() => {
    if (isArmed) {
      setWasArmed(true)
      setCancelled(false)
      return
    }
    if (wasArmed) {
      // Host approval or expiry ends arm polling. Re-read the grant before
      // rendering the terminal state, or a completed approval looks expired.
      void qc.invalidateQueries({ queryKey: ['ssh-agent-consent'] })
    }
  }, [isArmed, qc, wasArmed])

  const granted = !!view?.granted
  // A grant that landed closes the "request ended" story: without this reset,
  // approve-then-Revoke in one page view would show the expiry note for a
  // request that was in fact approved.
  useEffect(() => {
    if (granted) {
      setWasArmed(false)
      setCancelled(false)
    }
  }, [granted])
  const busy = isLoading || arm.isPending || revoke.isPending
  const approveCommand = armedView?.approve_command || 'kirocrew ssh-agent approve'

  return (
    <SettingsCard>
      <div className="text-[13px] font-semibold text-text">{i18nT('pages.settings.securityPanel.ssh_agent_title')}</div>
      {/* Purpose FIRST, in body colour: a reader who meets the warning before
          the reason has no reason to ever press the control. Then the risk -- stated in the open, not behind an InfoTip, because it is
          the whole reason this grant is a consent rather than a default. */}
      <div className="text-[12px] text-text mt-0.5 leading-relaxed">{i18nT('pages.settings.securityPanel.ssh_agent_when')}</div>
      <p className="text-[12px] text-muted mt-1 mb-2 leading-relaxed flex items-start gap-1">
        <AlertTriangle size={12} className="shrink-0 mt-[3px] text-warn" aria-hidden="true" />
        <span>{i18nT('pages.settings.securityPanel.ssh_agent_risk')}</span>
      </p>

      {/* The row renders only from a successful read, so a failed one shows
          neither "Not allowed" nor an Allow control (see `view` above). */}
      {view && (
        <div
          data-testid="ssh-agent-row"
          className="flex flex-col gap-1.5 border border-border rounded-md px-3 py-2"
        >
          <div className="flex items-start gap-2">
            <div className="flex-1 min-w-0">
              <div className="text-[12px] text-text flex items-center gap-1.5 flex-wrap">
                <span>{i18nT('pages.settings.securityPanel.ssh_agent_row_label')}</span>
                {/* The badge copy is shared with the file-delivery row on purpose:
                    same control grammar, same words, one key for translators. */}
                {granted
                  ? <Badge variant="ok">{i18nT('pages.settings.securityPanel.file_delivery_state_confirmed')}</Badge>
                  : isArmed
                    // Card-owned: the delivery row's "Waiting for approval" reads here
                    // as if someone ELSE approves, but this step-up is the owner's own
                    // terminal command.
                    ? <Badge variant="muted">{i18nT('pages.settings.securityPanel.ssh_agent_waiting_badge')}</Badge>
                    : <Badge variant="muted">{i18nT('pages.settings.securityPanel.file_delivery_state_not_confirmed')}</Badge>}
              </div>
              {/* On a tier that leaves ~/.ssh readable the spawn keeps the socket
                  with no grant (#18541), so "Not allowed" and Withdraw would read
                  as stopping a forward they do not stop. Said in body colour,
                  beside the badge, in every state. */}
              {view.forwarded_without_consent && (
                <div className="text-[11px] text-text mt-0.5 leading-relaxed" data-testid="ssh-agent-tier-forwards">
                  {i18nT('pages.settings.securityPanel.ssh_agent_tier_forwards')}
                </div>
              )}
              {!view.socket_present && (
                <div className="text-[11px] text-muted mt-0.5 leading-relaxed" data-testid="ssh-agent-no-socket">
                  {i18nT('pages.settings.securityPanel.ssh_agent_no_socket')}
                </div>
              )}
              {granted && (
                <div className="text-[11px] text-muted mt-0.5">
                  {/* Two lines, not one: the shared "Since …" key carries no
                      terminal punctuation, so run together the two sentences read
                      as one. */}
                  {/* A grant with no timestamp was written to the settings file
                      outside this page; say so, or two "Allowed" rows look like
                      two different states for no visible reason. */}
                  {view.granted_at
                    ? <div>{i18nT('pages.settings.securityPanel.file_delivery_since', { time: fmtDateTime(view.granted_at) })}</div>
                    : <div>{i18nT('pages.settings.securityPanel.ssh_agent_allowed_by_file')}</div>}
                  {/* The grant is read at agent spawn; a running session keeps
                      whatever it started with. Said here so Allow is never read
                      as having changed the session the owner is looking at. */}
                  <div>{i18nT('pages.settings.securityPanel.ssh_agent_applies_new_sessions')}</div>
                </div>
              )}
            </div>
            {granted
              ? (
                <Btn disabled={busy} onClick={() => revoke.mutate()} data-testid="ssh-agent-revoke">
                  {i18nT('pages.settings.securityPanel.ssh_agent_revoke')}
                </Btn>
              )
              : isArmed
                // Armed: the next action is running the host command, not
                // clicking again. The badge already says "Waiting for approval",
                // and a disabled button in the slot read as a dead control, so
                // the slot is simply empty.
                ? null
                : (
                  // Disabled while the gateway sees no ssh-agent: a grant made
                  // then forwards nothing, and the note above says what to do
                  // first. (The grant itself is read at spawn, so nothing is
                  // lost by waiting.)
                  <Btn
                    primary
                    disabled={busy || !view.socket_present}
                    onClick={() => arm.mutate()}
                    data-testid="ssh-agent-allow"
                  >
                    {i18nT('pages.settings.securityPanel.ssh_agent_allow')}
                  </Btn>
                )}
          </div>

          {/* Resting state: "Allow" reads as an immediate grant to a blind reader.
              It only ARMS a reversible two-step request; say so at the point of
              consent. */}
          {!granted && !isArmed && view.socket_present && (
            <div className="text-[11px] text-muted mt-0.5 leading-relaxed">
              {i18nT('pages.settings.securityPanel.ssh_agent_allow_help')}
            </div>
          )}

          {/* Armed but not yet approved: the grant is deliberately NOT recorded
              by the click. Show the host command that finishes it, plus Cancel,
              which discards the pending request without the step-up. */}
          {!granted && isArmed && (
            <div
              data-testid="ssh-agent-armed"
              className="rounded-md border border-border bg-bg-hover px-2.5 py-2 mt-0.5"
            >
              <div className="text-[11px] text-text leading-relaxed">
                {i18nT('pages.settings.securityPanel.ssh_agent_armed_help')}
              </div>
              <div className="text-[11px] text-muted leading-relaxed">
                {i18nT('pages.settings.securityPanel.ssh_agent_armed_what')}
              </div>
              <div className="flex items-center gap-2 mt-1 flex-wrap">
                <code className="flex-1 min-w-0 truncate text-[11px] font-mono text-text bg-bg rounded px-2 py-1 select-all" title={approveCommand}>
                  {approveCommand}
                </code>
                <Btn
                  // Acknowledge only on RESOLUTION, for the same reason the
                  // delivery card does: a false "Copied" lets the owner paste
                  // stale text into a 10-minute window.
                  onClick={() => {
                    setCmdCopyFailed(false)
                    copyToClipboard(approveCommand).then(
                      ok => { if (ok) setCmdCopied(true); else setCmdCopyFailed(true) },
                      () => { setCmdCopied(false); setCmdCopyFailed(true) },
                    )
                  }}
                  aria-label={i18nT('pages.settings.securityPanel.file_delivery_copy_command')}
                >
                  {cmdCopied ? <Check size={12} /> : <Copy size={12} />}
                  {cmdCopied
                    ? i18nT('pages.settings.securityPanel.file_delivery_copied')
                    : i18nT('pages.settings.securityPanel.file_delivery_copy')}
                </Btn>
                <Btn
                  disabled={busy}
                  // Marked cancelled only once the DELETE SUCCEEDS: a failed cancel
                  // leaves the request armed and approvable, and when it later
                  // expires the card must not claim the owner cancelled it.
                  onClick={() => revoke.mutate(undefined, { onSuccess: () => setCancelled(true) })}
                  data-testid="ssh-agent-cancel"
                >
                  {i18nT('pages.settings.securityPanel.ssh_agent_cancel')}
                </Btn>
              </div>
              {cmdCopyFailed && (
                <ErrorNotice
                  variant="inline"
                  className="mt-1.5"
                  message={i18nT('pages.settings.securityPanel.file_delivery_copy_failed')}
                  askAgent
                  onDismiss={() => setCmdCopyFailed(false)}
                />
              )}
              {typeof armedView?.expires_in === 'number' && (
                <div className="text-[11px] text-muted mt-1">
                  {i18nT('pages.settings.securityPanel.file_delivery_armed_expires', {
                    duration: fmtDuration(
                      [[Math.max(1, Math.round(armedView.expires_in / 60)), 'minute']],
                      { unitDisplay: 'long' },
                    ),
                  })}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* A request armed earlier this session has vanished (expired, cancelled
          or consumed) and left no recorded grant: say so rather than letting
          the command box silently unmount. */}
      {wasArmed && !isArmed && view && !granted && (
        <p className="text-[11px] text-muted mt-2 leading-relaxed" data-testid="ssh-agent-ended">
          {cancelled
            ? i18nT('pages.settings.securityPanel.ssh_agent_request_cancelled')
            : i18nT('pages.settings.securityPanel.ssh_agent_armed_expired')}
        </p>
      )}

      {isError && (
        <ErrorNotice variant="inline" className="mt-1.5" message={i18nT('pages.settings.securityPanel.ssh_agent_load_failed')} askAgent />
      )}
      {(arm.isError || revoke.isError) && (
        <ErrorNotice variant="inline" className="mt-1.5" message={i18nT('pages.settings.securityPanel.file_delivery_save_failed')} askAgent />
      )}
      {armed.isError && (
        <ErrorNotice variant="inline" className="mt-1.5" message={i18nT('pages.settings.securityPanel.file_delivery_arm_status_failed')} askAgent />
      )}
    </SettingsCard>
  )
}