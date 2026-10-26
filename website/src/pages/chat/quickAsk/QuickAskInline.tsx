import { ChevronRight } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import { askIcon, askTitle } from './QuickAskParts'
import type { QuickAsk } from './types'

/**
 * V1, folded: the one-line hint a surface shows while the ask's sheet is
 * folded away (Esc, Not now) or open on the other surface. One click opens
 * the sheet here.
 */
export default function QuickAskInline({ ask, onOpen }: { ask: QuickAsk; onOpen: () => void }) {
  const Icon = askIcon(ask.kind)
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label={`${i18nT('pages.chat.quickAsk.waiting_hint')}: ${askTitle(ask)}`}
      data-testid="quick-ask-inline"
      className="flex w-full items-center gap-2 border-0 border-t border-accent/50 bg-accent-subtle/60 px-3 py-2 text-left text-[13px] text-text-strong cursor-pointer hover:bg-accent-subtle"
    >
      <Icon size={14} aria-hidden="true" className="shrink-0 text-accent" />
      <span className="min-w-0 flex-1 font-medium">{i18nT('pages.chat.quickAsk.waiting_hint')}</span>
      <span className="inline-flex shrink-0 items-center gap-0.5 font-semibold text-accent">
        {i18nT('pages.chat.quickAsk.open')}
        <ChevronRight size={14} aria-hidden="true" />
      </span>
    </button>
  )
}
