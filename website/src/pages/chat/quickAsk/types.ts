/**
 * Quick asks: the few things a browsing agent needs a person for (sign in,
 * prove they are human, a one-time code, approve a purchase), offered as a
 * fixed set of typed asks the person clicks, instead of free chat.
 *
 * DESIGN MOCK (QA-1). Nothing here is wired to the gateway or CDP yet; the
 * shapes are what the three render variants share.
 *
 * Trust model the UI carries:
 *   - `origin` is the page's real address as the browser reports it, never
 *     text the agent or the page wrote. It is shown on every ask.
 *   - `kind` is one of four fixed values; the TITLE for each is ours (i18n),
 *     the agent only supplies `reason`, one short line, sanitized, plain text, capped at 80.
 *   - Secret values (password, code) are never handed back to the agent: the
 *     gateway writes them into the bound element and the frame masks it.
 *   - A CAPTCHA is never solved by the agent; the ask hands the person a
 *     focused takeover of just that control.
 */
export type QuickAskKind = 'signin' | 'captcha' | 'otp' | 'purchase'

/** One page element the ask writes to or points at, in frame pixels. */
export interface QuickAskTarget {
  field: 'username' | 'password' | 'otp' | 'checkbox' | 'submit'
  rect: { x: number; y: number; w: number; h: number }
}

export interface QuickAsk {
  id: string
  kind: QuickAskKind
  /** Page origin from the browser, e.g. `https://github.com`. */
  origin: string
  /** Agent-authored, one line. Sanitized and cut to REASON_MAX on display. */
  reason: string
  targets: QuickAskTarget[]
  /** Frame size the target rects are measured in. */
  frame: { w: number; h: number }
  /** signin: a username remembered for this origin only. */
  rememberedUsername?: string
  /** purchase: the amount and item as read from the page. */
  amount?: string
  item?: string
  /** purchase: the bound button's own text, read from the DOM, not from the agent. */
  button?: string
}

export const REASON_MAX = 80

/** Where the ask is drawn; the same body adapts its density. */
export type QuickAskDensity = 'inline' | 'sheet' | 'reply'
