import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef } from 'react'

import { browserLiveStore } from '../../hooks/useBrowserLive'
import type { DecodedBrowserFrame } from '../../hooks/useBrowserLive.frame'
import {
  isHandBackKey, keyToEvent, pointerToEvent, wheelToEvent,
  type BrowserInputEvent, type FrameSize,
} from '../../hooks/useBrowserLive.input'

/**
 * The picture in the live-browser card: a `<canvas>` painted from the frame
 * ring OUTSIDE React's render cycle, so 60 frames a second cost no React work.
 *
 * Painting. Each frame is decoded with `createImageBitmap` and drawn; while a
 * decode is in flight the newest arrival waits and every frame between is
 * dropped, so a slow machine shows the latest picture rather than falling
 * behind. The password-field boxes the relay reports are painted over AFTER
 * the frame, in the card's own background colour, so a password never shows
 * even for one frame. Nothing paints while the card is off screen: the owner
 * tells this component so through `visible`.
 *
 * Takeover. While `interactive`, pointer, wheel and key events on the canvas
 * are mapped to frame pixels (`useBrowserLive.input`) and posted to the
 * gateway. Mouse moves are coalesced to one in flight per animation frame;
 * downs and ups flush the pending move first so order is kept. Esc hands the
 * browser back and never reaches the page. Keystrokes are posted and
 * forgotten: nothing here logs, stores or renders what was typed.
 *
 * Replay. `replay(frames)` repaints a recorded sequence with its original
 * spacing (gaps capped so a long pause does not stall the replay); a live
 * frame or `stop()` ends it.
 */

export interface BrowserLiveCanvasHandle {
  /** Replay a recorded frame sequence; resolves when it ends or is stopped. */
  replay: (frames: readonly DecodedBrowserFrame[]) => Promise<void>
  /** Stop a replay and show the newest live frame again. */
  stop: () => void
  /** Draw one frame now (the Done row's "Last frame"). */
  show: (frame: DecodedBrowserFrame) => void
  /** The size of the last frame drawn, or null before the first. */
  frameSize: () => FrameSize | null
  focus: () => void
}

interface Props {
  sessionKey: string
  turnId: string
  /** Paint only while true; an off-screen card decodes nothing. */
  visible: boolean
  /** Route input to the browser (the person is in control). */
  interactive: boolean
  /** Esc, or any other hand-back gesture the canvas owns. */
  onHandBack?: () => void
  /** Hover-to-take-over affordance: a click while live claims input. */
  onClaim?: () => void
  /** Told when the drawn frame's pixel size changes, so the box can take its ratio. */
  onFrameSize?: (size: FrameSize) => void
  className?: string
  'aria-label': string
}

/** The longest gap a replay keeps between two frames. */
const REPLAY_MAX_GAP_MS = 500
const REPLAY_MIN_GAP_MS = 16

/** A frame's JPEG as something a 2D context can draw (also the replay's in-memory frames). */
export async function decode(frame: DecodedBrowserFrame): Promise<ImageBitmap | HTMLImageElement> {
  const blob = new Blob([frame.jpeg as BlobPart], { type: 'image/jpeg' })
  if (typeof createImageBitmap === 'function') return createImageBitmap(blob)
  // A host without createImageBitmap (an old shim, the test DOM): the Image route.
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob)
    const img = new Image()
    img.onload = () => { URL.revokeObjectURL(url); resolve(img) }
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('decode')) }
    img.src = url
  })
}

const BrowserLiveCardCanvas = forwardRef<BrowserLiveCanvasHandle, Props>(function BrowserLiveCardCanvas(
  { sessionKey, turnId, visible, interactive, onHandBack, onClaim, onFrameSize, className, 'aria-label': ariaLabel },
  ref,
) {
  const onFrameSizeRef = useRef(onFrameSize)
  onFrameSizeRef.current = onFrameSize
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const sizeRef = useRef<FrameSize | null>(null)
  const latestRef = useRef<DecodedBrowserFrame | null>(null)
  const pendingRef = useRef<DecodedBrowserFrame | null>(null)
  const decodingRef = useRef(false)
  const replayingRef = useRef(false)
  const replayTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const replayDone = useRef<(() => void) | null>(null)
  const visibleRef = useRef(visible)
  visibleRef.current = visible

  const maskColor = useCallback((): string => {
    const c = canvasRef.current
    if (!c) return '#000'
    const v = getComputedStyle(c).getPropertyValue('--card').trim()
    return v || '#000'
  }, [])

  const draw = useCallback(async (frame: DecodedBrowserFrame) => {
    const canvas = canvasRef.current
    if (!canvas) return
    const { w, h, mask } = frame.header
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w
      canvas.height = h
    }
    const prev = sizeRef.current
    sizeRef.current = { w, h }
    if (!prev || prev.w !== w || prev.h !== h) onFrameSizeRef.current?.({ w, h })
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    let img: ImageBitmap | HTMLImageElement
    try { img = await decode(frame) } catch { return }
    ctx.drawImage(img, 0, 0, w, h)
    if ('close' in img) img.close()
    if (mask.length) {
      ctx.fillStyle = maskColor()
      for (const [x, y, rw, rh] of mask) ctx.fillRect(x, y, rw, rh)
    }
  }, [maskColor])

  /** Paint the newest waiting frame; one decode in flight at a time. */
  const pump = useCallback(async () => {
    if (decodingRef.current) return
    decodingRef.current = true
    try {
      while (pendingRef.current) {
        const next = pendingRef.current
        pendingRef.current = null
        await draw(next)
      }
    } finally {
      decodingRef.current = false
    }
  }, [draw])

  const onLiveFrame = useCallback((frame: DecodedBrowserFrame) => {
    latestRef.current = frame
    if (replayingRef.current || !visibleRef.current) return
    pendingRef.current = frame
    void pump()
  }, [pump])

  useEffect(() => browserLiveStore.subscribeFrames(sessionKey, turnId, onLiveFrame), [sessionKey, turnId, onLiveFrame])

  // Coming back on screen paints the frame that arrived while hidden.
  useEffect(() => {
    if (visible && latestRef.current && !replayingRef.current) {
      pendingRef.current = latestRef.current
      void pump()
    }
  }, [visible, pump])

  const stopReplay = useCallback(() => {
    if (replayTimer.current) { clearTimeout(replayTimer.current); replayTimer.current = null }
    replayingRef.current = false
    replayDone.current?.()
    replayDone.current = null
    if (latestRef.current) { pendingRef.current = latestRef.current; void pump() }
  }, [pump])

  useImperativeHandle(ref, () => ({
    replay: (frames) => new Promise<void>((resolve) => {
      stopReplay()
      if (frames.length === 0) { resolve(); return }
      replayingRef.current = true
      replayDone.current = resolve
      let i = 0
      const step = () => {
        const f = frames[i]
        pendingRef.current = f
        void pump()
        i += 1
        if (i >= frames.length) {
          replayTimer.current = setTimeout(() => stopReplay(), REPLAY_MAX_GAP_MS)
          return
        }
        const gap = Math.min(REPLAY_MAX_GAP_MS, Math.max(REPLAY_MIN_GAP_MS, frames[i].receivedAt - f.receivedAt))
        replayTimer.current = setTimeout(step, gap)
      }
      step()
    }),
    stop: stopReplay,
    show: (frame) => { stopReplay(); pendingRef.current = frame; void pump() },
    frameSize: () => sizeRef.current,
    focus: () => canvasRef.current?.focus(),
  }), [pump, stopReplay])

  useEffect(() => () => { if (replayTimer.current) clearTimeout(replayTimer.current) }, [])

  // ---- takeover input -------------------------------------------------------

  const moveRef = useRef<BrowserInputEvent | null>(null)
  const moveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const send = useCallback((ev: BrowserInputEvent | null) => {
    if (!ev) return
    void browserLiveStore.sendInput(sessionKey, ev)
  }, [sessionKey])

  const flushMove = useCallback(() => {
    if (moveTimer.current) { clearTimeout(moveTimer.current); moveTimer.current = null }
    const m = moveRef.current
    moveRef.current = null
    send(m)
  }, [send])

  const box = () => {
    const r = canvasRef.current?.getBoundingClientRect()
    return r ? { left: r.left, top: r.top, width: r.width, height: r.height } : { left: 0, top: 0, width: 0, height: 0 }
  }

  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!interactive) return
    e.preventDefault()
    canvasRef.current?.focus()
    try { canvasRef.current?.setPointerCapture(e.pointerId) } catch { /* not a pointer device */ }
    flushMove()
    const size = sizeRef.current
    if (size) send(pointerToEvent('down', e.nativeEvent, box(), size, e.detail || 1))
  }
  const onPointerUp = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!interactive) return
    e.preventDefault()
    try { canvasRef.current?.releasePointerCapture(e.pointerId) } catch { /* not captured */ }
    flushMove()
    const size = sizeRef.current
    if (size) send(pointerToEvent('up', e.nativeEvent, box(), size, e.detail || 1))
  }
  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!interactive) return
    const size = sizeRef.current
    if (!size) return
    moveRef.current = pointerToEvent('move', e.nativeEvent, box(), size)
    if (!moveTimer.current) moveTimer.current = setTimeout(flushMove, 16)
  }
  // A keystroke meant for the page is the page's alone: `stopPropagation` keeps
  // it from the dashboard's window-level shortcut handler (Alt+digit switches
  // session, Alt+letter navigates), which would otherwise unmount this card
  // mid-takeover -- and on macOS Option+letter is how accents are typed.
  const onKeyDown = (e: React.KeyboardEvent<HTMLCanvasElement>) => {
    if (!interactive) return
    e.preventDefault()
    e.stopPropagation()
    if (isHandBackKey(e.key)) { onHandBack?.(); return }
    send(keyToEvent('down', e.nativeEvent))
  }
  const onKeyUp = (e: React.KeyboardEvent<HTMLCanvasElement>) => {
    if (!interactive) return
    e.preventDefault()
    e.stopPropagation()
    if (isHandBackKey(e.key)) return
    send(keyToEvent('up', e.nativeEvent))
  }

  // Wheel is registered natively: React's wheel listener is passive, so a
  // preventDefault there cannot stop the transcript from scrolling under the
  // person's hand while they scroll the page in the frame.
  useEffect(() => {
    const c = canvasRef.current
    if (!c || !interactive) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const size = sizeRef.current
      if (size) send(wheelToEvent(e, box(), size))
    }
    const onContextMenu = (e: MouseEvent) => e.preventDefault()
    c.addEventListener('wheel', onWheel, { passive: false })
    c.addEventListener('contextmenu', onContextMenu)
    return () => {
      c.removeEventListener('wheel', onWheel)
      c.removeEventListener('contextmenu', onContextMenu)
    }
  }, [interactive, send])

  useEffect(() => () => { if (moveTimer.current) clearTimeout(moveTimer.current) }, [])

  // Claiming: a plain click on the live frame. Keyboard users get the Take
  // over button in the header; the canvas itself is not a button while live.
  const onClick = () => { if (!interactive && onClaim) onClaim() }

  return (
    <canvas
      ref={canvasRef}
      role="img"
      aria-label={ariaLabel}
      tabIndex={interactive ? 0 : -1}
      data-testid="browser-live-canvas"
      data-interactive={interactive ? 'true' : undefined}
      className={`block w-full h-full object-contain bg-[var(--card)] outline-none select-none ${interactive ? 'cursor-default touch-none' : onClaim ? 'cursor-pointer' : ''} ${className ?? ''}`}
      onPointerDown={onPointerDown}
      onPointerUp={onPointerUp}
      onPointerMove={onPointerMove}
      onKeyDown={onKeyDown}
      onKeyUp={onKeyUp}
      onClick={onClick}
    />
  )
})

export default BrowserLiveCardCanvas
