/**
 * Isolated capture entry for the QA-1 quick-ask design variants.
 *
 * The REAL BrowserLiveCard (fed through the live-browser store, as in
 * capture/browser-live-card.tsx) and the REAL ChatInput composer, with each
 * variant's quick-ask component mounted in its slot. The page in the frame is
 * drawn here on a canvas so no gateway, relay or Chrome is needed. The
 * transcript rows above the card are plain scaffolding.
 *
 * ?v=1|2|3  &scene=signin|robot|otp|purchase  &theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { initI18n } from '../src/i18n'
import { browserLiveStore } from '../src/hooks/useBrowserLive'
import type { DecodedBrowserFrame } from '../src/hooks/useBrowserLive.frame'
import { useIsMobile } from '../src/hooks/useIsMobile'
import BrowserLiveCard from '../src/pages/chat/BrowserLiveCard'
import ChatInput from '../src/components/ChatInput'
import dashboardReducer from '../src/store/dashboardSlice'
import { createTestStore } from '../src/test/helpers'
import QuickAskInline from '../src/pages/chat/quickAsk/QuickAskInline'
import QuickAskSheet from '../src/pages/chat/quickAsk/QuickAskSheet'
import QuickAskReply from '../src/pages/chat/quickAsk/QuickAskReply'
import { QuickAskTargetRing } from '../src/pages/chat/quickAsk/QuickAskParts'
import type { QuickAsk } from '../src/pages/chat/quickAsk/types'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const v = params.get('v') || '1'
const scene = (params.get('scene') || 'signin') as 'signin' | 'robot' | 'otp' | 'purchase'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme)

const S = 'slot:quick-ask'
const FW = 1280
const FH = 800

browserLiveStore.__setWritesForTests({
  ack: async () => ({}), visibility: async () => ({}), claim: async () => ({}),
  release: async () => ({}), input: async () => ({}), open: async () => ({}),
})

// ---- The four scenes ---------------------------------------------------------

interface Scene {
  url: string
  title: string
  user: string
  agent: string
  step: string
  steps: string[]
  ask: QuickAsk
  paint: (g: CanvasRenderingContext2D) => void
}

const font = (px: number, weight = 400) => `${weight} ${px}px -apple-system, "Segoe UI", Helvetica, Arial, sans-serif`
function text(g: CanvasRenderingContext2D, s: string, x: number, y: number, px: number, color: string, weight = 400) {
  g.font = font(px, weight); g.fillStyle = color; g.fillText(s, x, y)
}
function box(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, fill: string, stroke?: string, r = 6) {
  g.beginPath(); g.roundRect(x, y, w, h, r); g.fillStyle = fill; g.fill()
  if (stroke) { g.strokeStyle = stroke; g.lineWidth = 1.5; g.stroke() }
}

function githubShell(g: CanvasRenderingContext2D, heading: string) {
  g.fillStyle = '#f6f8fa'; g.fillRect(0, 0, FW, FH)
  g.fillStyle = '#1f2328'; g.beginPath(); g.arc(320, 96, 30, 0, Math.PI * 2); g.fill()
  g.textAlign = 'center'; text(g, heading, 320, 180, 26, '#1f2328', 300); g.textAlign = 'left'
}

const SCENES: Record<string, Scene> = {
  signin: {
    url: 'https://github.com/login', title: 'Sign in to GitHub',
    user: 'Star the kirodotdev/KiroCrew repo on GitHub for me.',
    agent: 'Opening GitHub. You are signed out there, so I need you to sign in.',
    step: 'Waiting for you · sign in', steps: ['Open github.com', 'Sign in', 'Open the repo', 'Star it'],
    ask: {
      id: 'a1', kind: 'signin', origin: 'https://github.com', reason: 'Signing in so I can star kirodotdev/KiroCrew for you.',
      rememberedUsername: 'octocat', frame: { w: FW, h: FH },
      targets: [{ field: 'username', rect: { x: 176, y: 262, w: 288, h: 38 } }, { field: 'password', rect: { x: 176, y: 340, w: 288, h: 38 } }],
    },
    paint: (g) => {
      githubShell(g, 'Sign in to GitHub')
      box(g, 160, 210, 320, 250, '#ffffff', '#d0d7de')
      text(g, 'Username or email address', 176, 250, 15, '#1f2328', 500)
      box(g, 176, 262, 288, 38, '#ffffff', '#d0d7de')
      text(g, 'Password', 176, 328, 15, '#1f2328', 500)
      box(g, 176, 340, 288, 38, '#ffffff', '#d0d7de')
      box(g, 176, 400, 288, 38, '#1f883d'); g.textAlign = 'center'; text(g, 'Sign in', 320, 425, 15, '#ffffff', 600); g.textAlign = 'left'
      box(g, 160, 480, 320, 56, '#ffffff', '#d0d7de'); g.textAlign = 'center'; text(g, 'New to GitHub? Create an account.', 320, 514, 14, '#0969da'); g.textAlign = 'left'
    },
  },
  robot: {
    url: 'https://tickets.example.com/events/4412', title: 'One more step',
    user: 'Check if there are two seats left for Saturday on tickets.example.com.',
    agent: 'The ticket site wants a human check before it shows seats.',
    step: 'Waiting for you · human check', steps: ['Open the event', 'Human check', 'Read seat map', 'Report'],
    ask: {
      id: 'a2', kind: 'captcha', origin: 'https://tickets.example.com', reason: 'The ticket page asks for a human check before showing seats.',
      frame: { w: FW, h: FH }, targets: [{ field: 'checkbox', rect: { x: 120, y: 300, w: 380, h: 88 } }],
    },
    paint: (g) => {
      g.fillStyle = '#ffffff'; g.fillRect(0, 0, FW, FH)
      g.fillStyle = '#26215c'; g.fillRect(0, 0, FW, 64); text(g, 'tickets', 40, 42, 26, '#ffffff', 700)
      text(g, 'One more step', 120, 190, 32, '#111111', 700)
      text(g, 'Please confirm you are not a robot to see seats.', 120, 232, 17, '#444444')
      box(g, 120, 300, 380, 88, '#f9f9f9', '#d3d3d3', 4)
      box(g, 144, 329, 30, 30, '#ffffff', '#9e9e9e', 3)
      text(g, "I'm not a robot", 192, 352, 17, '#222222')
      g.fillStyle = '#9aa0a6'; g.beginPath(); g.arc(462, 334, 14, 0, Math.PI * 2); g.fill()
      text(g, 'Privacy · Terms', 428, 372, 10, '#777777')
    },
  },
  otp: {
    url: 'https://github.com/sessions/two-factor', title: 'Two-factor authentication',
    user: 'Star the kirodotdev/KiroCrew repo on GitHub for me.',
    agent: 'Signed in. GitHub now asks for your two-factor code.',
    step: 'Waiting for you · 6-digit code', steps: ['Open github.com', 'Sign in', 'Two-factor code', 'Star it'],
    ask: {
      id: 'a3', kind: 'otp', origin: 'https://github.com', reason: 'GitHub asked for your two-factor code to finish signing in.',
      frame: { w: FW, h: FH }, targets: [{ field: 'otp', rect: { x: 176, y: 290, w: 288, h: 42 } }],
    },
    paint: (g) => {
      githubShell(g, 'Two-factor authentication')
      box(g, 160, 210, 320, 220, '#ffffff', '#d0d7de')
      text(g, 'Authentication code', 176, 276, 15, '#1f2328', 500)
      box(g, 176, 290, 288, 42, '#ffffff', '#d0d7de'); text(g, 'XXXXXX', 188, 318, 15, '#8c959f')
      box(g, 176, 350, 288, 38, '#1f883d'); g.textAlign = 'center'; text(g, 'Verify', 320, 375, 15, '#ffffff', 600); g.textAlign = 'left'
      text(g, 'Open your two-factor authenticator app', 176, 412, 12, '#59636e')
    },
  },
  purchase: {
    url: 'https://shop.example.com/checkout', title: 'Checkout',
    user: 'Order two 3-packs of pocket notebooks from shop.example.com.',
    agent: 'The cart is ready. I will not place the order without your OK.',
    step: 'Waiting for you · approve payment', steps: ['Find notebooks', 'Add 2 to cart', 'Checkout', 'Place order'],
    ask: {
      id: 'a4', kind: 'purchase', origin: 'https://shop.example.com', reason: 'Ready to place the order you asked for.',
      amount: '$35.00', item: 'Pocket notebook, 3-pack × 2', button: 'Place order', frame: { w: FW, h: FH },
      targets: [{ field: 'submit', rect: { x: 120, y: 520, w: 380, h: 48 } }],
    },
    paint: (g) => {
      g.fillStyle = '#fbfaf7'; g.fillRect(0, 0, FW, FH)
      text(g, 'shop', 120, 70, 28, '#b45309', 800)
      text(g, 'Checkout', 120, 150, 30, '#111111', 700)
      box(g, 120, 180, 380, 310, '#ffffff', '#e5e2da', 8)
      box(g, 140, 204, 64, 64, '#fde68a', undefined, 6)
      text(g, 'Pocket notebook, 3-pack', 220, 230, 16, '#111111', 600)
      text(g, 'Qty 2', 220, 254, 14, '#666666')
      text(g, '$30.00', 420, 230, 16, '#111111')
      g.fillStyle = '#eeeeee'; g.fillRect(140, 300, 340, 1)
      text(g, 'Subtotal', 140, 334, 15, '#555555'); text(g, '$30.00', 420, 334, 15, '#111111')
      text(g, 'Shipping', 140, 366, 15, '#555555'); text(g, '$5.00', 428, 366, 15, '#111111')
      g.fillStyle = '#eeeeee'; g.fillRect(140, 392, 340, 1)
      text(g, 'Total', 140, 438, 20, '#111111', 700); text(g, '$35.00', 400, 438, 20, '#111111', 700)
      box(g, 120, 520, 380, 48, '#f59e0b', undefined, 8); g.textAlign = 'center'; text(g, 'Place order', 310, 551, 17, '#111111', 700); g.textAlign = 'left'
    },
  },
}

const sc = SCENES[scene]

async function frame(): Promise<DecodedBrowserFrame> {
  const c = document.createElement('canvas')
  c.width = FW; c.height = FH
  sc.paint(c.getContext('2d')!)
  const blob: Blob = await new Promise(r => c.toBlob(b => r(b!), 'image/jpeg', 0.85))
  return { header: { type: 'browser_frame', session_key: S, seq: 1, w: FW, h: FH, mask: [] }, jpeg: new Uint8Array(await blob.arrayBuffer()), receivedAt: 1000 }
}

const activeIndex = 1 + (scene === 'otp' || scene === 'purchase' ? 1 : 0)
browserLiveStore.applyEvent({
  session_key: S, turn_id: 'turn-1', url: sc.url, title: sc.title, browser: 'user_chrome', headless: false, rung: 0, fps: 30,
  state: 'live', step: { index: activeIndex, label: sc.step },
  steps: sc.steps.map((label, i) => ({ label, status: i < activeIndex ? 'done' : i === activeIndex ? 'active' : 'pending' })),
})
browserLiveStore.applyFrame(await frame())

// ---- The page ----------------------------------------------------------------

const store = createTestStore({ dashboard: { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true } })
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Transcript() {
  return (
    <div className="mx-auto w-full px-4 pt-6 pb-2 flex flex-col gap-3" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <div className="self-end max-w-[80%] rounded-2xl bg-bg-elevated border border-border px-4 py-2 text-[14px] text-text">{sc.user}</div>
      <div className="text-[14px] leading-6 text-text">{sc.agent}</div>
      <div className="text-[12px] text-muted">🔧 @kirocrew-core/browser</div>
    </div>
  )
}

function App() {
  const isMobile = useIsMobile()
  const ask = sc.ask
  const frameOverlay = v === '2' && !isMobile ? <QuickAskSheet ask={ask} mode="overlay" /> : <QuickAskTargetRing ask={ask} />
  const askSlot = v === '1' ? <QuickAskInline ask={ask} /> : undefined
  return (
    <div data-capture-root className="flex h-screen flex-col bg-bg text-text" style={{ ['--mc-content-width' as string]: '900px' }}>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <Transcript />
        <BrowserLiveCard sessionKey={S} anchorKey="call:quick-ask" anchorTs={Date.now()} running frameOverlay={frameOverlay} askSlot={askSlot} />
      </div>
      <div className="mx-auto w-full px-4 pb-3" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
        <ChatInput
          value=""
          onChange={() => {}}
          onSend={() => {}}
          isRunning
          onStop={() => {}}
          placeholder={v === '3' ? 'Or tell Kiro something else…' : 'Reply to Kiro…'}
          aboveComposer={v === '3' ? <QuickAskReply ask={ask} /> : undefined}
          typedCommandMenus={false}
          slotApprovalChrome={false}
          promptOptimizer={false}
          connected
        />
      </div>
      {v === '2' && isMobile && <QuickAskSheet ask={ask} mode="bottom" />}
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter><App /></MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
await new Promise(r => setTimeout(r, 600))
document.documentElement.setAttribute('data-capture-ready', '1')
