import { useId, useState } from 'react'
import { EyeOff, KeyRound, Lock, MousePointerClick, ShieldCheck, ShoppingCart, X } from 'lucide-react'

import { Btn } from '../../../components/ui'
import InfoTip from '../../../components/InfoTip'
import { i18nT } from '../../../i18n/t'
import { sanitizeLlmOutput } from '../../../utils/sanitize'
import { REASON_MAX, type QuickAsk, type QuickAskDensity, type QuickAskKind, type QuickAskTarget } from './types'

/**
 * Pieces every quick-ask variant shares: the fixed title, the origin line, the
 * agent's one-line reason, the "Kiro never sees it" hint, and the form body
 * per kind. The variants (inline, sheet, reply) only choose the container and
 * the density; the trust copy is the same everywhere.
 */

/** `https://github.com/login` -> `github.com`. Origins only, never a path. */
export function originHost(origin: string): string {
  try { return new URL(origin).host } catch { return origin }
}

export function askIcon(kind: QuickAskKind) {
  switch (kind) {
    case 'signin': return KeyRound
    case 'captcha': return ShieldCheck
    case 'otp': return Lock
    case 'purchase': return ShoppingCart
  }
}

/** Our title for the kind. The agent never writes this line. */
export function askTitle(ask: QuickAsk): string {
  const host = originHost(ask.origin)
  switch (ask.kind) {
    case 'signin': return i18nT('pages.chat.quickAsk.title_signin', { host })
    case 'captcha': return i18nT('pages.chat.quickAsk.title_captcha')
    case 'otp': return i18nT('pages.chat.quickAsk.title_otp')
    case 'purchase': return i18nT('pages.chat.quickAsk.title_purchase', { amount: ask.amount ?? '' })
  }
}

function fieldWord(f: QuickAskTarget['field']): string {
  switch (f) {
    case 'username': return i18nT('pages.chat.quickAsk.field_username')
    case 'password': return i18nT('pages.chat.quickAsk.field_password')
    case 'otp': return i18nT('pages.chat.quickAsk.field_otp')
    case 'checkbox': return i18nT('pages.chat.quickAsk.field_checkbox')
    case 'submit': return i18nT('pages.chat.quickAsk.field_approval')
  }
}

/** "github.com wants: username, password", with a lock for https. */
export function OriginLine({ ask, className = '' }: { ask: QuickAsk; className?: string }) {
  const fields = ask.targets.filter(t => t.field !== 'submit').map(t => fieldWord(t.field))
  const what = ask.kind === 'purchase' ? i18nT('pages.chat.quickAsk.field_approval') : fields.join(', ')
  return (
    <div className={`flex min-w-0 items-center gap-1.5 text-[12px] ${className}`} data-testid="quick-ask-origin">
      <Lock size={12} aria-hidden="true" className="shrink-0 text-ok" />
      {/* The host never truncates: it is the one thing a person must read. */}
      <span className="shrink-0 font-semibold text-text-strong">{originHost(ask.origin)}</span>
      <span className="min-w-0 truncate">
        <span className="text-muted">{i18nT('pages.chat.quickAsk.wants')} </span>
        <span className="font-medium text-text">{what}</span>
      </span>
      <InfoTip text={i18nT('pages.chat.quickAsk.origin_tip')} />
    </div>
  )
}

/** The agent's own words: one line, quoted, never a link, never the title. */
export function ReasonLine({ ask, className = '' }: { ask: QuickAsk; className?: string }) {
  const text = sanitizeLlmOutput(ask.reason).replace(/\s+/g, ' ').trim().slice(0, REASON_MAX)
  if (!text) return null
  return (
    <p className={`min-w-0 line-clamp-2 text-[12px] text-muted ${className}`} data-testid="quick-ask-reason">
      <span className="font-medium">{i18nT('pages.chat.quickAsk.reason_label')}</span> “{text}”
    </p>
  )
}

export function SecretHint({ ask, className = '' }: { ask: QuickAsk; className?: string }) {
  return (
    <div className={`flex flex-col gap-0.5 text-[12px] text-muted ${className}`} data-testid="quick-ask-secret-hint">
      <p className="flex items-center gap-1.5">
        <EyeOff size={12} aria-hidden="true" className="shrink-0 text-accent" />
        {i18nT('pages.chat.quickAsk.secret_hint')}
      </p>
      {/* OTP relay: a code from another site typed here signs someone else in. */}
      {ask.kind === 'otp' && <p className="pl-[18px]">{i18nT('pages.chat.quickAsk.otp_only_this_site', { host: originHost(ask.origin) })}</p>}
    </div>
  )
}

const inputBase = 'w-full min-w-0 rounded-md border border-border bg-bg px-2.5 text-text placeholder:text-muted focus:outline-none focus:border-accent focus:ring-2 focus:ring-accent/30'

function inputSize(d: QuickAskDensity) {
  return d === 'inline' ? 'h-8 text-[13px]' : d === 'sheet' ? 'h-11 text-[15px]' : 'h-9 text-[14px]'
}

function Field({ label, children, density, hideLabel }: { label: string; children: (id: string) => React.ReactNode; density: QuickAskDensity; hideLabel?: boolean }) {
  const id = useId()
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-1">
      <label htmlFor={id} className={hideLabel ? 'sr-only' : `font-medium text-text ${density === 'sheet' ? 'text-[13px]' : 'text-[12px]'}`}>{label}</label>
      {children(id)}
    </div>
  )
}

/** The form body for one ask. `density` picks sizes and the stacking. */
export function QuickAskFields({ ask, density, autoFocus }: { ask: QuickAsk; density: QuickAskDensity; autoFocus?: boolean }) {
  const [remember, setRemember] = useState(!!ask.rememberedUsername)
  const [username, setUsername] = useState('')
  const stack = density === 'sheet' ? 'flex-col' : 'flex-col sm:flex-row'
  const hideLabel = density === 'inline'
  switch (ask.kind) {
    case 'signin':
      return (
        <div className="flex flex-col gap-2">
          <div className={`flex ${stack} gap-2`}>
            <Field label={i18nT('pages.chat.quickAsk.label_username')} density={density} hideLabel={hideLabel}>
              {id => (
                <div className="flex min-w-0 gap-1.5">
                  <input id={id} aria-label={i18nT('pages.chat.quickAsk.label_username')} className={`${inputBase} ${inputSize(density)}`} autoComplete="username" spellCheck={false}
                    value={username} onChange={e => setUsername(e.target.value)} placeholder={i18nT('pages.chat.quickAsk.label_username')} />
                  {/* A saved value is never filled on its own: one click each time. */}
                  {ask.rememberedUsername && !username && (
                    <Btn className={`shrink-0 whitespace-nowrap ${inputSize(density)}`} onClick={() => setUsername(ask.rememberedUsername ?? '')} data-testid="quick-ask-use-saved">
                      {i18nT('pages.chat.quickAsk.use_saved', { value: ask.rememberedUsername })}
                    </Btn>
                  )}
                </div>
              )}
            </Field>
            <Field label={i18nT('pages.chat.quickAsk.label_password')} density={density} hideLabel={hideLabel}>
              {id => (
                <input id={id} aria-label={i18nT('pages.chat.quickAsk.label_password')} type="password" className={`${inputBase} ${inputSize(density)}`} autoComplete="current-password"
                  autoFocus={autoFocus} placeholder={i18nT('pages.chat.quickAsk.label_password')} data-testid="quick-ask-password" />
              )}
            </Field>
          </div>
          <label className="flex w-fit cursor-pointer items-center gap-2 text-[12px] text-text">
            <input type="checkbox" aria-label={i18nT('pages.chat.quickAsk.remember_username', { host: originHost(ask.origin) })} className="accent-[var(--accent)]" checked={remember} onChange={e => setRemember(e.target.checked)} />
            {i18nT('pages.chat.quickAsk.remember_username', { host: originHost(ask.origin) })}
          </label>
        </div>
      )
    case 'otp':
      return (
        <Field label={i18nT('pages.chat.quickAsk.label_otp')} density={density} hideLabel={hideLabel}>
          {id => (
            <input id={id} aria-label={i18nT('pages.chat.quickAsk.label_otp')} className={`${inputBase} ${inputSize(density)} ${density === 'sheet' ? 'text-center text-[22px]' : ''} font-mono tracking-[0.4em]`}
              inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} autoFocus={autoFocus}
              placeholder="000000" data-testid="quick-ask-otp" />
          )}
        </Field>
      )
    case 'captcha':
      return (
        <p className="flex items-start gap-2 text-[13px] leading-5 text-text">
          <MousePointerClick size={15} aria-hidden="true" className="mt-0.5 shrink-0 text-accent" />
          {i18nT('pages.chat.quickAsk.captcha_explainer')}
        </p>
      )
    case 'purchase':
      return (
        <dl className={`grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 rounded-lg border border-border bg-bg px-3 py-2 ${density === 'sheet' ? 'text-[14px]' : 'text-[13px]'}`} data-testid="quick-ask-purchase">
          <dt className="text-muted">{i18nT('pages.chat.quickAsk.purchase_amount')}</dt>
          <dd className={`font-semibold text-text-strong ${density === 'sheet' ? 'text-[20px] leading-7' : ''}`}>{ask.amount}</dd>
          <dt className="text-muted">{i18nT('pages.chat.quickAsk.purchase_merchant')}</dt>
          <dd className="flex items-center gap-1 font-medium text-text"><Lock size={12} aria-hidden="true" className="text-ok" />{originHost(ask.origin)}</dd>
          {ask.item && (<><dt className="text-muted">{i18nT('pages.chat.quickAsk.purchase_item')}</dt><dd className="truncate text-text">{sanitizeLlmOutput(ask.item).slice(0, 80)}</dd></>)}
          {ask.button && (<><dt className="text-muted">{i18nT('pages.chat.quickAsk.purchase_button')}</dt><dd className="text-text">{i18nT('pages.chat.quickAsk.purchase_press_once', { button: ask.button.slice(0, 40) })}</dd></>)}
        </dl>
      )
  }
}

/** The ask's primary action, by kind. */
export function primaryLabel(ask: QuickAsk): string {
  switch (ask.kind) {
    case 'signin': return i18nT('pages.chat.quickAsk.fill_in_page')
    case 'otp': return i18nT('pages.chat.quickAsk.fill_code')
    case 'captcha': return i18nT('pages.chat.quickAsk.captcha_takeover')
    case 'purchase': return i18nT('pages.chat.quickAsk.purchase_confirm', { amount: ask.amount ?? '' })
  }
}

export function QuickAskActions({ ask, density, onDecline }: { ask: QuickAsk; density: QuickAskDensity; onDecline?: () => void }) {
  const big = density === 'sheet' ? 'h-11 justify-center text-[15px] px-4' : ''
  return (
    <div className={`flex items-center gap-2 ${density === 'sheet' ? 'flex-col-reverse items-stretch' : ''}`}>
      <Btn className={`${big} ${density === 'inline' ? '!px-2' : ''}`} onClick={onDecline} data-testid="quick-ask-decline">
        {density === 'inline' ? <X size={14} aria-label={i18nT('pages.chat.quickAsk.not_now')} /> : i18nT('pages.chat.quickAsk.not_now')}
      </Btn>
      <Btn primary className={big} data-testid="quick-ask-primary">{primaryLabel(ask)}</Btn>
    </div>
  )
}

/** A ring on the page element the ask is bound to, drawn over the frame. */
export function QuickAskTargetRing({ ask, dim }: { ask: QuickAsk; dim?: boolean }) {
  return (
    <div aria-hidden="true" className={`pointer-events-none absolute inset-0 ${dim ? 'bg-black/45' : ''}`} data-testid="quick-ask-target">
      {ask.targets.filter(t => t.field !== 'submit' || ask.kind === 'purchase').map(t => (
        <div
          key={t.field}
          className="absolute rounded-md ring-2 ring-accent shadow-[0_0_0_4px_var(--accent-glow)]"
          style={{
            left: `${(t.rect.x / ask.frame.w) * 100}%`, top: `${(t.rect.y / ask.frame.h) * 100}%`,
            width: `${(t.rect.w / ask.frame.w) * 100}%`, height: `${(t.rect.h / ask.frame.h) * 100}%`,
          }}
        />
      ))}
    </div>
  )
}
