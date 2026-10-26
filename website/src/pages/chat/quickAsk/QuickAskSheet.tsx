import { useEffect, useRef } from 'react'

import { i18nT } from '../../../i18n/t'
import { askIcon, askTitle, OriginLine, QuickAskActions, QuickAskFields, QuickAskTargetRing, ReasonLine, SecretHint } from './QuickAskParts'
import type { QuickAsk } from './types'

/**
 * V2 -- the focused ask sheet (Muse-style). The agent pauses and one dialog
 * holds the whole ask. On a wide screen it sits over the live frame, which
 * dims except for a ring on the bound element, so the person sees exactly
 * what will be filled. On a phone it is a bottom sheet over the chat.
 *
 * A modal dialog: focus moves into it and Escape is "Not now".
 */
export default function QuickAskSheet({ ask, mode }: { ask: QuickAsk; mode: 'overlay' | 'bottom' }) {
  const Icon = askIcon(ask.kind)
  const secret = ask.kind === 'signin' || ask.kind === 'otp'
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => { ref.current?.focus() }, [])

  const panel = (
    <div
      ref={ref}
      role="dialog"
      aria-modal="true"
      aria-label={askTitle(ask)}
      tabIndex={-1}
      data-testid="quick-ask-sheet"
      className={mode === 'overlay'
        ? 'w-[min(400px,calc(100%-32px))] rounded-2xl border border-border-strong bg-bg-elevated p-5 shadow-2xl focus:outline-none'
        : 'w-full rounded-t-2xl border-t border-border-strong bg-bg-elevated px-5 pb-[max(20px,env(safe-area-inset-bottom))] pt-2 shadow-2xl focus:outline-none'}
    >
      {mode === 'bottom' && <div aria-hidden="true" className="mx-auto mb-3 h-1 w-10 rounded-full bg-border-strong" />}
      <div className="flex items-start gap-3">
        <div className="grid size-10 shrink-0 place-items-center rounded-xl bg-accent-subtle text-accent">
          <Icon size={20} aria-hidden="true" />
        </div>
        <div className="min-w-0 flex-1">
          <h2 className="text-[16px] font-semibold leading-6 text-text-strong">{askTitle(ask)}</h2>
          <OriginLine ask={ask} className="mt-0.5" />
        </div>
      </div>
      <ReasonLine ask={ask} className="mt-3" />
      <div className="mt-3"><QuickAskFields ask={ask} density="sheet" /></div>
      {secret && <SecretHint ask={ask} className="mt-3" />}
      <div className="mt-4"><QuickAskActions ask={ask} density="sheet" /></div>
      <p className="mt-3 text-center text-[11px] text-muted">{i18nT('pages.chat.quickAsk.kiro_paused')}</p>
    </div>
  )

  if (mode === 'overlay') {
    return (
      <div className="absolute inset-0 z-20" data-testid="quick-ask-overlay">
        <QuickAskTargetRing ask={ask} dim />
        {/* Right-aligned so the ringed element on the page stays in sight. */}
        <div className="absolute inset-0 flex items-center justify-end p-4">{panel}</div>
      </div>
    )
  }
  return (
    <div className="fixed inset-0 z-50 flex flex-col justify-end bg-black/50" data-testid="quick-ask-bottom">
      {panel}
    </div>
  )
}
