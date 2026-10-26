/**
 * Takeover input mapping: card pointer, wheel and key events to the
 * `POST /api/browser/input/event` body the relay turns into
 * `Input.dispatchMouseEvent` / `Input.dispatchKeyEvent`.
 *
 * Pure functions over plain numbers and the few DOM-event fields they need, so
 * the mapping is tested without a canvas. Coordinates are FRAME pixels (the
 * JPEG's own width and height), never CSS pixels: the canvas is drawn to fill
 * its box while keeping the frame's aspect ratio, so a point is scaled by the
 * box-to-frame ratio and refused when it lands in the letterbox.
 *
 * Nothing here logs: a key event carries what the person typed into a page
 * the agent cannot see, and it goes to the gateway and nowhere else.
 */

/** The wire body. `action` tells a mouse down from up from move, a key down from up. */
export interface BrowserInputEvent {
  type: 'mouse' | 'wheel' | 'key'
  action?: 'down' | 'up' | 'move' | 'press'
  x?: number
  y?: number
  /** CDP button name. */
  button?: 'left' | 'middle' | 'right' | 'none'
  clicks?: number
  dx?: number
  dy?: number
  key?: string
  code?: string
  /** Printable text for a key down, when the key produces one. */
  text?: string
  /** CDP modifier bitmask: Alt 1, Ctrl 2, Meta 4, Shift 8. */
  modifiers?: number
}

export interface Box { left: number; top: number; width: number; height: number }
export interface FrameSize { w: number; h: number }

/** The CDP modifier bits. */
export const MOD_ALT = 1
export const MOD_CTRL = 2
export const MOD_META = 4
export const MOD_SHIFT = 8

export interface ModifierSource { altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }

export function modifiersOf(e: ModifierSource): number {
  return (e.altKey ? MOD_ALT : 0) | (e.ctrlKey ? MOD_CTRL : 0) | (e.metaKey ? MOD_META : 0) | (e.shiftKey ? MOD_SHIFT : 0)
}

/**
 * Where the frame is drawn inside the canvas box: the largest rectangle of
 * the frame's aspect ratio that fits, centred. The letterbox is whatever is
 * left over.
 */
export function drawnRect(box: Box, frame: FrameSize): Box {
  if (box.width <= 0 || box.height <= 0 || frame.w <= 0 || frame.h <= 0) return { left: box.left, top: box.top, width: 0, height: 0 }
  const scale = Math.min(box.width / frame.w, box.height / frame.h)
  const width = frame.w * scale
  const height = frame.h * scale
  return { left: box.left + (box.width - width) / 2, top: box.top + (box.height - height) / 2, width, height }
}

/**
 * Map a client-space point to frame pixels, or `null` when it falls in the
 * letterbox or the box has no size. Rounded to whole pixels, clamped to the
 * frame's last pixel so an edge click still lands.
 *
 * With `clamp`, a point outside the drawn frame is pulled to its nearest edge
 * instead of refused. A press is never clamped (a click in the letterbox is not
 * a click on the page), but a RELEASE must be: pointer capture delivers the up
 * to the canvas wherever the pointer went, and dropping it would leave the
 * page's mouse button held (every later move a drag, the next click a second
 * press) until a click happened to land inside the frame.
 */
export function mapPointToFrame(clientX: number, clientY: number, box: Box, frame: FrameSize, clamp = false): { x: number; y: number } | null {
  const r = drawnRect(box, frame)
  if (r.width <= 0 || r.height <= 0) return null
  const outside = clientX < r.left || clientY < r.top || clientX > r.left + r.width || clientY > r.top + r.height
  if (outside && !clamp) return null
  const x = Math.min(frame.w - 1, Math.max(0, Math.round(((clientX - r.left) / r.width) * frame.w)))
  const y = Math.min(frame.h - 1, Math.max(0, Math.round(((clientY - r.top) / r.height) * frame.h)))
  return { x, y }
}

export interface PointerLike extends ModifierSource {
  clientX: number
  clientY: number
  /** DOM button: 0 left, 1 middle, 2 right. */
  button: number
  /** Buttons currently held (DOM bitmask: 1 left, 2 right, 4 middle). */
  buttons?: number
}

export function buttonName(domButton: number): BrowserInputEvent['button'] {
  switch (domButton) {
    case 0: return 'left'
    case 1: return 'middle'
    case 2: return 'right'
    default: return 'none'
  }
}

/** The held button a move should report, from the DOM `buttons` bitmask. */
function heldButton(buttons: number | undefined): BrowserInputEvent['button'] {
  if (!buttons) return 'none'
  if (buttons & 1) return 'left'
  if (buttons & 2) return 'right'
  if (buttons & 4) return 'middle'
  return 'none'
}

/**
 * A pointer down, up or move. `clicks` is the click count the browser should
 * see (1, or 2 for a double click), counted by the caller from the DOM
 * `detail`. Returns `null` for a DOWN in the letterbox; an UP, and a MOVE while
 * a button is held (a drag that left the frame), are clamped to the frame's
 * edge so the page always sees the release where the drag ended.
 */
export function pointerToEvent(action: 'down' | 'up' | 'move', e: PointerLike, box: Box, frame: FrameSize, clicks = 1): BrowserInputEvent | null {
  const clamp = action === 'up' || (action === 'move' && !!e.buttons)
  const p = mapPointToFrame(e.clientX, e.clientY, box, frame, clamp)
  if (!p) return null
  return {
    type: 'mouse',
    action,
    x: p.x,
    y: p.y,
    button: action === 'move' ? heldButton(e.buttons) : buttonName(e.button),
    clicks: action === 'move' ? 0 : Math.max(1, Math.min(3, clicks)),
    modifiers: modifiersOf(e),
  }
}

export interface WheelLike extends ModifierSource {
  clientX: number
  clientY: number
  deltaX: number
  deltaY: number
  /** DOM deltaMode: 0 pixel, 1 line, 2 page. */
  deltaMode?: number
}

const LINE_PX = 16
const PAGE_PX = 800

/** A wheel tick in frame pixels, so a line- or page-mode wheel scrolls a sane amount. */
export function wheelToEvent(e: WheelLike, box: Box, frame: FrameSize): BrowserInputEvent | null {
  const p = mapPointToFrame(e.clientX, e.clientY, box, frame)
  if (!p) return null
  const unit = e.deltaMode === 1 ? LINE_PX : e.deltaMode === 2 ? PAGE_PX : 1
  return {
    type: 'wheel',
    x: p.x,
    y: p.y,
    dx: Math.round(e.deltaX * unit),
    dy: Math.round(e.deltaY * unit),
    modifiers: modifiersOf(e),
  }
}

export interface KeyLike extends ModifierSource {
  key: string
  code: string
}

/** A key that stands for itself when typed. */
function printable(key: string): boolean {
  return key.length === 1
}

/**
 * A key down or up. `text` rides along only on a down of a printable key with
 * no Ctrl or Meta held, which is what the browser needs to insert a character.
 */
export function keyToEvent(action: 'down' | 'up', e: KeyLike): BrowserInputEvent {
  const modifiers = modifiersOf(e)
  const out: BrowserInputEvent = { type: 'key', action, key: e.key, code: e.code, modifiers }
  if (action === 'down' && printable(e.key) && !(modifiers & (MOD_CTRL | MOD_META))) out.text = e.key
  return out
}

// ---- touch typing ------------------------------------------------------------
//
// A canvas cannot raise a phone's keyboard. After a tap the card asks what took
// focus in the page; for a field it focuses a hidden textarea of its own (the
// "proxy") so the keyboard opens, and forwards what the keyboard produces.

/** The gateway's answer to "what took focus" after a release. */
export interface FieldFocus {
  editable: boolean
  /** The field's type (`number`, `tel`, `textarea`, ...); `frame` for a cross-origin frame. */
  kind: string
  /** The field's own `inputmode` attribute, when it has one. */
  mode: string
}

/** Read `focus` off an input-event reply; `null` when there is none. */
export function focusOfReply(reply: unknown): FieldFocus | null {
  if (!reply || typeof reply !== 'object') return null
  const f = (reply as { focus?: unknown }).focus
  if (!f || typeof f !== 'object') return null
  const { editable, kind, mode } = f as Record<string, unknown>
  return { editable: editable === true, kind: typeof kind === 'string' ? kind : '', mode: typeof mode === 'string' ? mode : '' }
}

/** The keyboard the proxy should ask for: the page field's own inputmode first, else by type. */
export function keyboardHints(focus: FieldFocus): { inputMode: string; enterKeyHint: string } {
  const byKind: Record<string, string> = { number: 'decimal', tel: 'tel', email: 'email', url: 'url', search: 'search' }
  const inputMode = focus.mode && focus.mode !== 'none' ? focus.mode : byKind[focus.kind] ?? 'text'
  const enterKeyHint = focus.kind === 'textarea' || focus.kind === 'frame' ? 'enter' : focus.kind === 'search' ? 'search' : 'go'
  return { inputMode, enterKeyHint }
}

/** The wire caps typed text at 64 characters an event (`takeover.MAX_TEXT_CHARS`). */
export const TEXT_CHUNK = 64

/** Committed text as keyless text events (`Input.insertText`), split to the wire cap. */
export function textToEvents(text: string): BrowserInputEvent[] {
  const out: BrowserInputEvent[] = []
  const chars = Array.from(text)
  for (let i = 0; i < chars.length; i += TEXT_CHUNK) {
    out.push({ type: 'key', action: 'press', key: '', text: chars.slice(i, i + TEXT_CHUNK).join(''), modifiers: 0 })
  }
  return out
}

function namedKey(key: string, code: string): BrowserInputEvent[] {
  return [
    { type: 'key', action: 'down', key, code, modifiers: 0 },
    { type: 'key', action: 'up', key, code, modifiers: 0 },
  ]
}

/**
 * A `beforeinput` on the proxy as wire events, or `null` to let the proxy take
 * it (a composition in progress: the IME owns the text until it commits).
 */
export function beforeInputToEvents(inputType: string, data: string | null, composing: boolean): BrowserInputEvent[] | null {
  if (composing || inputType === 'insertCompositionText') return null
  switch (inputType) {
    case 'insertText':
    case 'insertReplacementText':
    case 'insertFromPaste':
    case 'insertFromDrop':
      return data ? textToEvents(data) : []
    case 'insertLineBreak':
    case 'insertParagraph':
      return namedKey('Enter', 'Enter')
    case 'deleteContentBackward':
    case 'deleteWordBackward':
      return namedKey('Backspace', 'Backspace')
    case 'deleteContentForward':
    case 'deleteWordForward':
      return namedKey('Delete', 'Delete')
    default:
      return []
  }
}

export interface ProxyKeyLike extends KeyLike {
  isComposing?: boolean
  keyCode?: number
}

/**
 * Whether a key down on the proxy goes to the page as a KEY. Printable keys do
 * not: their character arrives as `beforeinput` and is sent as text, so sending
 * the key too would type it twice. Neither does an IME's placeholder key
 * (`Unidentified`/`Process`, keyCode 229, Android's soft keyboard for every
 * key): its `beforeinput` says what it did. Named keys (Backspace on iOS, Enter,
 * Tab, arrows) and Ctrl/Meta chords do.
 */
export function proxyKeyIsForwarded(e: ProxyKeyLike): boolean {
  if (e.isComposing || e.keyCode === 229 || e.key === 'Unidentified' || e.key === 'Process' || e.key === 'Dead') return false
  if (printable(e.key) && !(modifiersOf(e) & (MOD_CTRL | MOD_META))) return false
  return true
}

/** Keys the card keeps for itself while the person is in control. */
export function isHandBackKey(key: string): boolean {
  return key === 'Escape'
}
