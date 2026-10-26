import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { createRef } from 'react'

import BrowserLiveCardCanvas, { type BrowserLiveCanvasHandle } from '../pages/chat/BrowserLiveCardCanvas'
import { browserLiveStore } from '../hooks/useBrowserLive'
import type { DecodedBrowserFrame } from '../hooks/useBrowserLive.frame'

/**
 * The painter: frames reach the canvas only while visible, the newest frame
 * wins when decodes queue up, password boxes are painted over after the
 * image, replay walks the ring on the clock, and takeover input is mapped to
 * frame pixels and posted (moves coalesced, Esc kept for hand-back).
 */

const S = 'slot:abc'
const frame = (seq: number, over: Partial<DecodedBrowserFrame['header']> = {}): DecodedBrowserFrame => ({
  header: { type: 'browser_frame', session_key: S, seq, w: 640, h: 400, mask: [], ...over },
  jpeg: new Uint8Array([seq]),
  receivedAt: 1000 + seq * 100,
})

let ctx: { drawImage: ReturnType<typeof vi.fn>; fillRect: ReturnType<typeof vi.fn>; fillStyle: string }
let decodes: Array<{ resolve: (v: unknown) => void }>
let writes: { input: ReturnType<typeof vi.fn> }
let restore: ReturnType<typeof browserLiveStore.__setWritesForTests>

function flushDecodes() { for (const d of decodes.splice(0)) d.resolve({ close: vi.fn() }) }

beforeEach(() => {
  browserLiveStore.__resetForTests()
  browserLiveStore.applyEvent({ session_key: S, turn_id: 't1', state: 'live', url: 'https://x.test/' })
  writes = { input: vi.fn(async () => ({ ok: true })) }
  restore = browserLiveStore.__setWritesForTests({ ...writes, ack: vi.fn(async () => ({ ok: true })) })
  ctx = { drawImage: vi.fn(), fillRect: vi.fn(), fillStyle: '' }
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(ctx as unknown as CanvasRenderingContext2D)
  decodes = []
  vi.stubGlobal('createImageBitmap', vi.fn(() => new Promise(resolve => { decodes.push({ resolve }) })))
  vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 320, height: 200, right: 320, bottom: 200, x: 0, y: 0, toJSON: () => ({}) })
})
afterEach(() => {
  browserLiveStore.__setWritesForTests(restore)
  browserLiveStore.__resetForTests()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

const mount = (over: Partial<React.ComponentProps<typeof BrowserLiveCardCanvas>> = {}) => {
  const ref = createRef<BrowserLiveCanvasHandle>()
  const utils = render(<BrowserLiveCardCanvas ref={ref} sessionKey={S} turnId="t1" visible interactive={false} aria-label="frame" {...over} />)
  return { ref, ...utils }
}

describe('painting', () => {
  it('sizes the canvas to the frame and draws it once decoded', async () => {
    mount()
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas') as HTMLCanvasElement
    expect(canvas.width).toBe(640)
    expect(canvas.height).toBe(400)
    expect(ctx.drawImage).not.toHaveBeenCalled()
    await act(async () => { flushDecodes(); await Promise.resolve() })
    expect(ctx.drawImage).toHaveBeenCalledTimes(1)
  })

  it('paints the password boxes over the image, in the card colour', async () => {
    mount()
    act(() => browserLiveStore.applyFrame(frame(1, { mask: [[10, 20, 100, 30], [0, 0, 5, 5]] })))
    await act(async () => { flushDecodes(); await Promise.resolve() })
    expect(ctx.fillRect.mock.calls).toEqual([[10, 20, 100, 30], [0, 0, 5, 5]])
    // drawImage first, then the mask: the order the spy saw them in.
    expect(ctx.drawImage.mock.invocationCallOrder[0]).toBeLessThan(ctx.fillRect.mock.invocationCallOrder[0])
  })

  it('drops frames that arrive while a decode is in flight and paints the newest', async () => {
    mount()
    act(() => {
      browserLiveStore.applyFrame(frame(1))
      browserLiveStore.applyFrame(frame(2))
      browserLiveStore.applyFrame(frame(3))
    })
    expect(createImageBitmap).toHaveBeenCalledTimes(1)
    await act(async () => { flushDecodes(); await Promise.resolve(); await Promise.resolve() })
    // The second decode is frame 3; frame 2 was never decoded.
    expect(createImageBitmap).toHaveBeenCalledTimes(2)
    await act(async () => { flushDecodes(); await Promise.resolve() })
    expect(ctx.drawImage).toHaveBeenCalledTimes(2)
  })

  it('paints nothing while off screen, then the latest frame on return', async () => {
    const { rerender } = mount({ visible: false })
    act(() => { browserLiveStore.applyFrame(frame(1)); browserLiveStore.applyFrame(frame(2)) })
    expect(createImageBitmap).not.toHaveBeenCalled()
    rerender(<BrowserLiveCardCanvas sessionKey={S} turnId="t1" visible interactive={false} aria-label="frame" />)
    expect(createImageBitmap).toHaveBeenCalledTimes(1)
    const blob = (createImageBitmap as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0] as Blob
    expect(blob.size).toBe(1)
  })

  it('replay walks the frames on the clock and ends by itself', async () => {
    vi.useFakeTimers()
    const { ref } = mount()
    const frames = [frame(1), frame(2), frame(3)]
    let done = false
    act(() => { void ref.current!.replay(frames).then(() => { done = true }) })
    expect(createImageBitmap).toHaveBeenCalledTimes(1)
    await act(async () => { flushDecodes(); await Promise.resolve() })
    // Recorded 100 ms apart.
    await act(async () => { vi.advanceTimersByTime(100); flushDecodes(); await Promise.resolve() })
    expect(createImageBitmap).toHaveBeenCalledTimes(2)
    await act(async () => { vi.advanceTimersByTime(100); flushDecodes(); await Promise.resolve() })
    expect(createImageBitmap).toHaveBeenCalledTimes(3)
    expect(done).toBe(false)
    await act(async () => { vi.advanceTimersByTime(600); flushDecodes(); await Promise.resolve() })
    expect(done).toBe(true)
  })
})

describe('takeover input', () => {
  it('posts nothing while not interactive', () => {
    mount()
    act(() => browserLiveStore.applyFrame(frame(1)))
    fireEvent.pointerDown(screen.getByTestId('browser-live-canvas'), { clientX: 10, clientY: 10, button: 0 })
    fireEvent.keyDown(screen.getByTestId('browser-live-canvas'), { key: 'a' })
    expect(writes.input).not.toHaveBeenCalled()
  })

  it('maps a click to frame pixels through the 320x200 box', () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1 })
    fireEvent.pointerUp(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1 })
    expect(writes.input.mock.calls.map(c => c[1])).toEqual([
      expect.objectContaining({ type: 'mouse', action: 'down', x: 320, y: 200, button: 'left', clicks: 1 }),
      expect.objectContaining({ type: 'mouse', action: 'up', x: 320, y: 200, button: 'left' }),
    ])
  })

  it('coalesces moves to one per tick and flushes a pending move before a down', () => {
    vi.useFakeTimers()
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerMove(canvas, { clientX: 10, clientY: 10, buttons: 0 })
    fireEvent.pointerMove(canvas, { clientX: 20, clientY: 20, buttons: 0 })
    fireEvent.pointerMove(canvas, { clientX: 30, clientY: 30, buttons: 0 })
    expect(writes.input).not.toHaveBeenCalled()
    act(() => { vi.advanceTimersByTime(16) })
    expect(writes.input).toHaveBeenCalledTimes(1)
    expect(writes.input.mock.calls[0][1]).toMatchObject({ action: 'move', x: 60, y: 60 })
    fireEvent.pointerMove(canvas, { clientX: 40, clientY: 40, buttons: 0 })
    fireEvent.pointerDown(canvas, { clientX: 40, clientY: 40, button: 0, pointerId: 1 })
    expect(writes.input.mock.calls.slice(1).map(c => c[1].action)).toEqual(['move', 'down'])
  })

  it('a key goes to the page; Esc goes to hand-back and never to the page', () => {
    const onHandBack = vi.fn()
    mount({ interactive: true, onHandBack })
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.keyDown(canvas, { key: 'Enter', code: 'Enter' })
    fireEvent.keyUp(canvas, { key: 'Enter', code: 'Enter' })
    fireEvent.keyDown(canvas, { key: 'Escape', code: 'Escape' })
    fireEvent.keyUp(canvas, { key: 'Escape', code: 'Escape' })
    expect(writes.input.mock.calls.map(c => c[1])).toEqual([
      expect.objectContaining({ type: 'key', action: 'down', key: 'Enter' }),
      expect.objectContaining({ type: 'key', action: 'up', key: 'Enter' }),
    ])
    expect(onHandBack).toHaveBeenCalledTimes(1)
  })

  it('a keystroke typed into the page never reaches the window-level shortcut handler', () => {
    // useKeyboardShortcuts listens on `window`; an Alt+digit that bubbled there
    // would switch session and unmount the card mid-takeover.
    const windowSaw = vi.fn()
    window.addEventListener('keydown', windowSaw)
    window.addEventListener('keyup', windowSaw)
    try {
      mount({ interactive: true, onHandBack: vi.fn() })
      const canvas = screen.getByTestId('browser-live-canvas')
      fireEvent.keyDown(canvas, { key: '1', code: 'Digit1', altKey: true })
      fireEvent.keyUp(canvas, { key: '1', code: 'Digit1', altKey: true })
      fireEvent.keyDown(canvas, { key: 'Escape', code: 'Escape' })
      expect(writes.input.mock.calls.map(c => c[1])).toEqual([
        expect.objectContaining({ type: 'key', action: 'down', key: '1', modifiers: 1 }),
        expect.objectContaining({ type: 'key', action: 'up', key: '1' }),
      ])
      expect(windowSaw).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('keydown', windowSaw)
      window.removeEventListener('keyup', windowSaw)
    }
  })

  it('a release past the drawn frame still posts, clamped to the edge', () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1 })
    fireEvent.pointerUp(canvas, { clientX: 900, clientY: -30, button: 0, pointerId: 1 })
    const actions = writes.input.mock.calls.map(c => c[1])
    expect(actions.at(-1)).toMatchObject({ type: 'mouse', action: 'up', x: 639, y: 0, button: 'left' })
  })

  it('a wheel tick is posted with the pointer position and the deltas', () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    // happy-dom's WheelEvent drops the pointer fields, so the event is built by hand.
    const ev = new Event('wheel', { cancelable: true, bubbles: true })
    for (const [k, v] of Object.entries({ clientX: 160, clientY: 100, deltaX: 0, deltaY: 40, deltaMode: 0, altKey: false, ctrlKey: false, metaKey: false, shiftKey: false })) {
      Object.defineProperty(ev, k, { value: v })
    }
    canvas.dispatchEvent(ev)
    expect(ev.defaultPrevented).toBe(true)
    expect(writes.input.mock.calls[0][1]).toMatchObject({ type: 'wheel', x: 320, y: 200, dx: 0, dy: 40 })
  })

  const tap = (canvas: HTMLElement) => {
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 7, pointerType: 'touch' })
    fireEvent.pointerUp(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 7, pointerType: 'touch' })
  }

  it('a tap on a page field opens the phone keyboard with the field\'s keyboard type', async () => {
    writes.input.mockImplementation(async (_k: string, ev: { action?: string }) =>
      ev.action === 'up' ? { sent: 1, focus: { editable: true, kind: 'number', mode: '' } } : { sent: 1 })
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLTextAreaElement
    tap(canvas)
    // Focused inside the tap itself: a phone opens its keyboard only then.
    expect(document.activeElement).toBe(proxy)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(document.activeElement).toBe(proxy)
    expect(proxy.inputMode).toBe('decimal')
    expect(writes.input.mock.calls.map(c => c[1].action)).toEqual(['down', 'up'])
  })

  it('a tap on something that takes no typing closes the keyboard again', async () => {
    writes.input.mockImplementation(async () => ({ sent: 1, focus: { editable: false, kind: '', mode: '' } }))
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    tap(screen.getByTestId('browser-live-canvas'))
    const proxy = screen.getByTestId('browser-live-type-proxy')
    expect(document.activeElement).toBe(proxy)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(document.activeElement).not.toBe(proxy)
  })

  it('a mouse click never focuses the keyboard proxy', () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1, pointerType: 'mouse' })
    fireEvent.pointerUp(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1, pointerType: 'mouse' })
    expect(document.activeElement).toBe(canvas)
  })

  const beforeInput = (el: HTMLElement, inputType: string, data: string | null) => {
    const ev = new Event('beforeinput', { bubbles: true, cancelable: true })
    Object.defineProperty(ev, 'inputType', { value: inputType })
    Object.defineProperty(ev, 'data', { value: data })
    Object.defineProperty(ev, 'isComposing', { value: false })
    el.dispatchEvent(ev)
    return ev
  }

  it('what the phone keyboard types goes to the page as text, Backspace and Enter', () => {
    mount({ interactive: true })
    const proxy = screen.getByTestId('browser-live-type-proxy')
    // Android: every soft key is keyCode 229, so the key itself is not sent.
    fireEvent.keyDown(proxy, { key: 'Unidentified', keyCode: 229 })
    expect(beforeInput(proxy, 'insertText', '7').defaultPrevented).toBe(true)
    beforeInput(proxy, 'deleteContentBackward', null)
    beforeInput(proxy, 'insertLineBreak', null)
    const ce = new Event('compositionend', { bubbles: true })
    Object.defineProperty(ce, 'data', { value: 'hello' })
    proxy.dispatchEvent(ce)
    expect(writes.input.mock.calls.map(c => c[1])).toEqual([
      { type: 'key', action: 'press', key: '', text: '7', modifiers: 0 },
      expect.objectContaining({ type: 'key', action: 'down', key: 'Backspace' }),
      expect.objectContaining({ type: 'key', action: 'up', key: 'Backspace' }),
      expect.objectContaining({ type: 'key', action: 'down', key: 'Enter' }),
      expect.objectContaining({ type: 'key', action: 'up', key: 'Enter' }),
      { type: 'key', action: 'press', key: '', text: 'hello', modifiers: 0 },
    ])
  })

  it('keys on the proxy: named keys go as keys, printable ones once (as text), none reach the window', () => {
    const windowSaw = vi.fn()
    window.addEventListener('keydown', windowSaw)
    try {
      mount({ interactive: true })
      const proxy = screen.getByTestId('browser-live-type-proxy')
      fireEvent.keyDown(proxy, { key: '5', code: 'Digit5', keyCode: 53 })
      fireEvent.keyDown(proxy, { key: 'Backspace', code: 'Backspace', keyCode: 8 })
      fireEvent.keyUp(proxy, { key: 'Backspace', code: 'Backspace', keyCode: 8 })
      expect(writes.input.mock.calls.map(c => [c[1].action, c[1].key])).toEqual([['down', 'Backspace'], ['up', 'Backspace']])
      expect(windowSaw).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('keydown', windowSaw)
    }
  })

  it('a click on the live (non-interactive) frame claims', () => {
    const onClaim = vi.fn()
    mount({ onClaim })
    fireEvent.click(screen.getByTestId('browser-live-canvas'))
    expect(onClaim).toHaveBeenCalledTimes(1)
  })
})
