import { i18nT } from '../../../i18n/t'
import { askIcon, askTitle, OriginLine, QuickAskActions, QuickAskFields, ReasonLine, SecretHint } from './QuickAskParts'
import type { QuickAsk } from './types'

/**
 * V3 -- the structured reply. The ask docks on the chat composer (ChatInput's
 * `aboveComposer` slot), the place a person already answers the agent. The
 * card in the transcript keeps the live picture and a ring on the bound
 * element; the composer's own text box stays usable for "do something else".
 */
export default function QuickAskReply({ ask }: { ask: QuickAsk }) {
  const Icon = askIcon(ask.kind)
  const secret = ask.kind === 'signin' || ask.kind === 'otp'
  return (
    <section
      role="group"
      aria-label={askTitle(ask)}
      data-testid="quick-ask-reply"
      className="mb-2 rounded-xl border border-accent/60 bg-card p-3 shadow-sm"
    >
      <div className="flex items-center gap-2">
        <Icon size={15} aria-hidden="true" className="shrink-0 text-accent" />
        <span className="text-[14px] font-semibold text-text-strong">{askTitle(ask)}</span>
        <span className="flex-1" />
        <span className="hidden whitespace-nowrap text-[11px] text-muted sm:inline">{i18nT('pages.chat.quickAsk.reply_to_kiro')}</span>
      </div>
      <OriginLine ask={ask} className="mt-1" />
      <ReasonLine ask={ask} className="mt-1" />
      <div className="mt-2.5 flex flex-col gap-2 sm:flex-row sm:items-end">
        <div className="min-w-0 flex-1"><QuickAskFields ask={ask} density="reply" /></div>
        <QuickAskActions ask={ask} density="reply" />
      </div>
      {secret && <SecretHint ask={ask} className="mt-2" />}
    </section>
  )
}
