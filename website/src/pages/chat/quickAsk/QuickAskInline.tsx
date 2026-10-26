import { i18nT } from '../../../i18n/t'
import { askIcon, askTitle, OriginLine, QuickAskActions, QuickAskFields, ReasonLine, SecretHint } from './QuickAskParts'
import type { QuickAsk } from './types'

/**
 * V1 -- inline ask strip. Lives INSIDE the live-browser card, between the frame
 * and the step strip: the picture stays in view and the ask is one compact
 * band under it. Lowest footprint; the person answers without leaving the card.
 */
export default function QuickAskInline({ ask }: { ask: QuickAsk }) {
  const Icon = askIcon(ask.kind)
  const secret = ask.kind === 'signin' || ask.kind === 'otp'
  return (
    <section
      role="group"
      aria-label={askTitle(ask)}
      data-testid="quick-ask-inline"
      className="border-t border-accent/50 bg-accent-subtle/60 px-3 py-2"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full bg-accent px-2 py-0.5 text-[12px] font-semibold text-accent-fg">
          <Icon size={12} aria-hidden="true" />
          {i18nT('pages.chat.quickAsk.waiting_for_you')}
        </span>
        <span className="text-[13px] font-semibold text-text-strong">{askTitle(ask)}</span>
        <OriginLine ask={ask} className="min-w-0 basis-full sm:basis-auto sm:flex-1" />
      </div>
      <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:items-start">
        <div className="min-w-0 flex-1"><QuickAskFields ask={ask} density="inline" /></div>
        <QuickAskActions ask={ask} density="inline" />
      </div>
      <div className="mt-1.5 flex flex-col gap-0.5 sm:flex-row sm:flex-wrap sm:items-center sm:gap-x-4">
        {secret && <SecretHint ask={ask} />}
        <ReasonLine ask={ask} className="sm:flex-1" />
      </div>
    </section>
  )
}
