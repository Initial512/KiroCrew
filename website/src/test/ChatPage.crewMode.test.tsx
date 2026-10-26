/**
 * Crew Mode on ChatPage: the switch sits right of the memory chip on a new chat,
 * turns the chat to the existing conductor through the ordinary agent switch, and
 * swaps the empty chat's welcome page for the Crew Mode one. Once pressed, both the
 * memory-mode chip and the Crew Mode chip leave the composer; until the first
 * message the composer's mark turns it off again, after that nothing does. WelcomeView is mocked to nothing, so
 * a Crew Mode page found here comes from ChatPage's own branch.
 */
import { afterEach, describe, it, expect, vi } from 'vitest'
import { render, screen, act, fireEvent, waitFor, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import chatReducer, { appendMessage } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

interface VirtuosoMockProps {
  data?: unknown[]
  itemContent: (index: number, item: unknown) => ReactNode
}
vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: VirtuosoMockProps) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))

type Msg = { role: string; content: string; meta?: Record<string, unknown> }
// `running` is the reply state the slot detail read reports; `hold` keeps that read
// pending, so the page stays in its loading state.
const detail = vi.hoisted(() => ({ messages: [] as Msg[], running: false, hold: false }))
const createChatSlot = vi.hoisted(() => vi.fn())
const deleteChatSlot = vi.hoisted(() => vi.fn().mockResolvedValue(undefined))
// An empty name is "use the default": the server stamps the default template.
const chatSlotAgent = vi.hoisted(() => vi.fn(async (_slot: string, agent: string) => ({ agent: agent || 'default', workspace: 'default' })))
const dashboardConfig = vi.hoisted(() => vi.fn().mockResolvedValue({}))
// The worker list read; by default it fails, which the breadcrumb says with a notice.
const crewWorkers = vi.hoisted(() => vi.fn((): Promise<unknown> => Promise.reject(new Error('no worker list'))))
vi.mock('../api/client', () => ({
  api: {
    createChatSlot,
    deleteChatSlot,
    chatSlotAgent,
    dashboardConfig,
    crewWorkers,
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn(() => detail.hold
      ? new Promise(() => {})
      // A copy: the store freezes the rows it was preloaded with.
      : Promise.resolve({ messages: structuredClone(detail.messages), running: detail.running, has_more: false, total: detail.messages.length })),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
const agentsState = vi.hoisted(() => ({ defaultAgent: 'default' }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: agentsState.defaultAgent }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }), emitSlotFocused: () => {} }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'
import { setCrewModeCatalog } from '../lib/crewMode'
import { openCrewSession } from '../pages/chat/command-center/CrewWorkerList'
// The surface registry is populated by module side effect, and only `App.tsx`
// imports it in production -- so a harness that mounts ChatPage directly starts
// with an EMPTY registry and every surface lookup misses. Import it here for the
// same reason the app does. (The miss degrades safely to the surface-free
// sentence, which is what the unregistered-surface case below asserts.)
import '../surfaces/builtins'

type Slot = {
  messages: Msg[]; mode?: string; agent?: string; agentKind?: string; origin?: string; memoryMode?: string; slotKeys?: string[]
  /** Who dispatched this session: set, it is not a root session and has no Overview. */
  createdBy?: string
  instanceId?: string
  /** The Crew Mode root this slot is a worker of. */
  crewRoot?: string
  slotRunning?: boolean
  /** The listed slot's `running`, which follows the server. */
  listedRunning?: boolean
  slotLoading?: boolean
  activityOpen?: boolean
  /** The worker's slot row says a tool approval waits. */
  pendingApproval?: boolean
}

function makeStore({ messages, mode = '', agent = 'default', agentKind = '', origin = 'user', memoryMode = 'persistent', slotKeys = ['slot-a'], createdBy, instanceId, crewRoot, slotRunning = false, listedRunning = false, slotLoading = false, activityOpen = false, pendingApproval = false }: Slot) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: slotKeys.map(key => ({ key, messages: key === 'slot-a' ? messages.length : 0, running: key === 'slot-a' && listedRunning, mode: key === 'slot-a' ? mode : '', agent: key === 'slot-a' ? agent : 'default', agent_kind: key === 'slot-a' ? agentKind : '', origin: key === 'slot-a' ? origin : 'user', memory_mode: key === 'slot-a' ? memoryMode : 'persistent', ...(key === 'slot-a' && createdBy ? { created_by: createdBy } : {}), ...(key === 'slot-a' && instanceId ? { instance_id: instanceId } : {}), ...(key === 'slot-a' && crewRoot ? { crew_root: crewRoot, title: 'Worker B' } : {}), ...(key === 'root-1' ? { title: 'Sample goal', crew_mode_root: true } : {}), pending_approval: key === 'slot-a' && pendingApproval, waiting_for_input: false, last_activity_ts: undefined })),
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages,
        slotRunning, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        unresumableResume: null, lastResumeRequestId: null,
        subagents: {}, toolLog: [], activityOpen, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderWith(slot: Slot) {
  detail.messages = slot.messages
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(slot)
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  return store
}

describe('Crew Mode on ChatPage', () => {
  afterEach(() => { crewWorkers.mockReset(); crewWorkers.mockImplementation(() => Promise.reject(new Error('no worker list'))); agentsState.defaultAgent = 'default'; detail.running = false; detail.hold = false; localStorage.clear(); setCrewModeCatalog(undefined, undefined) })

  it('sits right of the memory chip on the welcome state', async () => {
    await renderWith({ messages: [] })
    const row = screen.getByTestId('composer-memory-chip')
    const memory = within(row).getByTestId('memory-mode-chip')
    const crew = within(row).getByTestId('crew-mode-chip')
    expect(crew).toHaveAttribute('aria-pressed', 'false')
    expect(memory.compareDocumentPosition(crew) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByTestId('crew-mode-welcome')).toBeNull()
  })

  it('turns on through the ordinary agent switch to the conductor', async () => {
    const store = await renderWith({ messages: [] })
    fireEvent.click(screen.getByTestId('crew-mode-chip'))
    await waitFor(() => expect(chatSlotAgent).toHaveBeenCalledWith('slot-a', 'kirocrew-conductor', 'template'))
    await waitFor(() => expect(store.getState().dashboard.slots[0].agent).toBe('kirocrew-conductor'))
    expect(await screen.findByTestId('crew-mode-welcome')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-mode-chip')).toBeNull()
    expect(screen.queryByTestId('memory-mode-chip')).toBeNull()
  })

  it('switches to the conductor the gateway names, so swapping one config key swaps it', async () => {
    chatSlotAgent.mockClear()
    setCrewModeCatalog('better-conductor', ['kirocrew-conductor'])
    await renderWith({ messages: [] })
    fireEvent.click(screen.getByTestId('crew-mode-chip'))
    await waitFor(() => expect(chatSlotAgent).toHaveBeenCalledWith('slot-a', 'better-conductor', 'template'))
  })

  it('shows the Crew Mode chip, not a picker, on a conductor chat', async () => {
    await renderWith({ messages: [{ role: 'user', content: 'hello' }], agent: 'kirocrew-conductor', agentKind: 'template' })
    expect(screen.getByTestId('crew-mode-composer-chip')).toHaveTextContent('Crew Mode')
    const mark = screen.getByTestId('crew-mode-composer-chip')
    expect(mark.tagName).not.toBe('BUTTON')
    // A read-only status: named for what it says, not focusable, no pointer.
    expect(mark).toHaveAttribute('aria-label', 'Crew Mode is on for this chat')
    expect(mark).not.toHaveAttribute('tabindex')
    expect(mark).not.toHaveAttribute('title')
    expect(mark.className).toContain('cursor-default')
    expect(mark.className).not.toMatch(/\bborder\b|rounded|hover:/)
  })

  it('shows the Crew Mode badge on every conductor, the pipeline conductor included', async () => {
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor', 'kirocrew-pipeline-conductor'])
    await renderWith({ messages: [{ role: 'user', content: 'hello' }], agent: 'kirocrew-pipeline-conductor', agentKind: 'template' })
    expect(screen.getByTestId('crew-mode-composer-chip')).toHaveTextContent('Crew Mode')
  })

  it('opens a worker like a session: breadcrumb, back to conductor, and a steer composer', async () => {
    const store = await renderWith({ messages: [{ role: 'user', content: 'do part 2' }], agent: 'kirocrew-worker', agentKind: 'template', createdBy: 'root-1', crewRoot: 'root-1', slotKeys: ['slot-a', 'root-1'] })
    const crumb = screen.getByTestId('crew-worker-breadcrumb')
    expect(crumb).toHaveTextContent('Sample goal')
    expect(crumb).toHaveTextContent('Worker B')
    expect(screen.getByTestId('crew-steer-composer')).toBeInTheDocument()
    expect(screen.getByTestId('crew-steer-label')).toHaveTextContent('Steer · Worker B')
    // An idle worker: a send starts a new turn, it does not cut into a reply.
    expect(screen.getByTestId('crew-steer-hint')).toHaveTextContent('Starts a new turn in Worker B.')
    expect(screen.getByTestId('crew-steer-hint')).toHaveAttribute('data-running', 'false')
    fireEvent.click(screen.getByTestId('crew-back-to-conductor'))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('root-1'))
  })

  it('V1: a worker opened from Workers while Split View is on leaves the split and shows the worker', async () => {
    dashboardConfig.mockResolvedValue({ session_grid: true })
    const store = await renderWith({ messages: [{ role: 'user', content: 'do part 2' }], agent: 'kirocrew-worker', agentKind: 'template', createdBy: 'root-1', crewRoot: 'root-1', slotKeys: ['slot-a', 'root-1'] })
    fireEvent.click(screen.getByTestId('crew-back-to-conductor'))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('root-1'))
    // Split View on the conductor: the grid replaces the single chat.
    await act(async () => { fireEvent.keyDown(document, { key: 'd', ctrlKey: true }) })
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Split right' }).length).toBeGreaterThan(0))
    // The Workers panel's open action.
    await act(async () => { openCrewSession(store.dispatch, 'slot-a', true) })
    await waitFor(() => expect(screen.getByTestId('crew-worker-breadcrumb')).toHaveTextContent('Worker B'))
    expect(screen.getByTestId('crew-steer-composer')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Split right' })).toBeNull()
    dashboardConfig.mockResolvedValue({})
  })

  it("a running worker's steer hint says a send cuts into its current reply", async () => {
    detail.running = true
    await renderWith({ messages: [{ role: 'user', content: 'do part 2' }], agent: 'kirocrew-worker', agentKind: 'template', createdBy: 'root-1', crewRoot: 'root-1', slotKeys: ['slot-a', 'root-1'], listedRunning: true })
    await waitFor(() => expect(screen.getByTestId('crew-steer-hint')).toHaveTextContent('Cuts into its current reply.'))
    expect(screen.getByTestId('crew-steer-hint')).toHaveAttribute('data-running', 'true')
  })

  it("a done worker's steer hint says a send starts a new turn, even with a stale turn flag", async () => {
    crewWorkers.mockResolvedValue({ workers: [{ key: 'slot-a', title: 'Worker B', agent: 'kirocrew-worker', status: 'working', group: 'done', last_activity: '', ledger_item: '', created_by: 'root-1' }] })
    detail.running = true
    await renderWith({ messages: [{ role: 'user', content: 'do part 2' }], agent: 'kirocrew-worker', agentKind: 'template', createdBy: 'root-1', crewRoot: 'root-1', slotKeys: ['slot-a', 'root-1'], listedRunning: true })
    await waitFor(() => expect(screen.getByTestId('crew-steer-hint')).toHaveTextContent('Starts a new turn in Worker B.'))
    expect(screen.getByTestId('crew-steer-hint')).toHaveAttribute('data-running', 'false')
  })

  // ── edge review R2: Y1 / X2, Y2 / X5, Y3 ────────────────────────────────────

  const permission = (): Msg => ({ role: 'permission', content: 'Shell', meta: { approval_id: 'rid-1', tool_input: 'printf hi', request_id: 'rid-1' } })

  it("Y1: a worker's own composer offers no approval controls, only where to decide it", async () => {
    const store = await renderWith({ messages: [{ role: 'user', content: 'do part 2' }, permission()], agent: 'kirocrew-worker', agentKind: 'template', createdBy: 'root-1', crewRoot: 'root-1', slotKeys: ['slot-a', 'root-1'], pendingApproval: true })
    await waitFor(() => expect(screen.getByTestId('crew-worker-approval-waiting')).toHaveTextContent('Waiting for approval in Sample goal'))
    expect(screen.queryByText('Allow once')).toBeNull()
    expect(screen.queryByText('Trust all tools for this session')).toBeNull()
    expect(screen.queryByRole('button', { name: /^Reject/ })).toBeNull()
    fireEvent.click(screen.getByTestId('crew-worker-open-root'))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('root-1'))
  })

  it('Y1: a plain chat with the same pending approval still offers its own controls', async () => {
    await renderWith({ messages: [{ role: 'user', content: 'hello' }, permission()], pendingApproval: true })
    expect(await screen.findByText('Allow once')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-worker-approval-waiting')).toBeNull()
  })

  it('Y2: with its conductor closed the worker composer does not send and offers Reopen', async () => {
    // No 'root-1' row: the conductor's tab is closed.
    await renderWith({ messages: [{ role: 'user', content: 'do part 2' }], agent: 'kirocrew-worker', agentKind: 'template', createdBy: 'root-1', crewRoot: 'root-1', slotKeys: ['slot-a'] })
    const gate = await screen.findByTestId('crew-worker-root-closed')
    expect(gate).toHaveTextContent('to keep working')
    expect(screen.getByTestId('crew-worker-reopen-root')).toHaveTextContent('Reopen')
    const box = within(screen.getByTestId('crew-steer-composer')).getByRole('textbox')
    expect(box).toHaveAttribute('aria-disabled', 'true')
    expect(box).toHaveAttribute('readonly')
    // R5 V2: the reopen sentence is said once, not again in the header or the box.
    expect(screen.getAllByText(/to keep working|Reopen it to keep steering/)).toHaveLength(1)
    expect(box.getAttribute('placeholder') ?? '').toBe('')
    // R4 H4: only the reopen line, no 'Starts a new turn' hint.
    expect(screen.queryByTestId('crew-steer-hint')).toBeNull()
  })

  it('Y3: a reload under a closed conductor keeps its saved name and the real worker state', async () => {
    crewWorkers.mockResolvedValue({ root: 'root-1', root_closed: true, root_title: 'Toy Flag Review', workers: [{ key: 'slot-a', title: 'Worker B', agent: 'kirocrew-worker', status: 'idle', group: 'done', last_activity: '', ledger_item: 'it_1', created_by: 'root-1' }] })
    await renderWith({ messages: [{ role: 'user', content: 'do part 2' }], agent: 'kirocrew-worker', agentKind: 'template', createdBy: 'root-1', crewRoot: 'root-1', slotKeys: ['slot-a'] })
    await waitFor(() => expect(screen.getByTestId('crew-breadcrumb-root')).toHaveTextContent('Toy Flag Review'))
    expect(screen.getByTestId('crew-worker-breadcrumb')).toHaveTextContent('Done')
    expect(screen.getByTestId('crew-worker-root-closed')).toHaveTextContent('Reopen Toy Flag Review to keep working')
  })

  it('a plain chat has no breadcrumb and no steer composer', async () => {
    await renderWith({ messages: [{ role: 'user', content: 'hello' }] })
    expect(screen.queryByTestId('crew-worker-breadcrumb')).toBeNull()
    expect(screen.queryByTestId('crew-steer-composer')).toBeNull()
  })

  it('keeps the agent picker on a plain chat', async () => {
    await renderWith({ messages: [{ role: 'user', content: 'hello' }] })
    expect(screen.queryByRole('button', { name: /^Agent: default/ })).toBeInTheDocument()
  })

  it('offers no agent picker on a conductor chat', async () => {
    await renderWith({ messages: [{ role: 'user', content: 'hello' }], agent: 'kirocrew-conductor', agentKind: 'template' })
    expect(screen.queryByRole('button', { name: /^Agent: / })).toBeNull()
  })

  it('hides both chips the moment it is pressed, before the switch lands', async () => {
    let land: (v: { agent: string; workspace: string }) => void = () => {}
    chatSlotAgent.mockImplementationOnce(() => new Promise(r => { land = r }))
    const store = await renderWith({ messages: [] })
    fireEvent.click(screen.getByTestId('crew-mode-chip'))
    expect(screen.queryByTestId('crew-mode-chip')).toBeNull()
    expect(screen.queryByTestId('memory-mode-chip')).toBeNull()
    // The switch reaches the API a tick later (a lazily loaded helper); land it then.
    await waitFor(() => expect(chatSlotAgent).toHaveBeenCalledWith('slot-a', 'kirocrew-conductor', 'template'))
    await act(async () => { land({ agent: 'kirocrew-conductor', workspace: 'default' }) })
    await waitFor(() => expect(store.getState().dashboard.slots[0].agent).toBe('kirocrew-conductor'))
    expect(screen.queryByTestId('crew-mode-chip')).toBeNull()
    expect(screen.queryByTestId('memory-mode-chip')).toBeNull()
  })

  it('keeps one welcome mounted from the press on, before and after the switch lands', async () => {
    let land: (v: { agent: string; workspace: string }) => void = () => {}
    chatSlotAgent.mockImplementationOnce(() => new Promise(r => { land = r }))
    const store = await renderWith({ messages: [] })
    fireEvent.click(screen.getByTestId('crew-mode-chip'))
    // At once, while the switch is still in flight and the slot record still names
    // the default agent: the Crew Mode page, not the plain welcome.
    const welcome = await screen.findByTestId('crew-mode-welcome')
    expect(store.getState().dashboard.slots[0].agent).toBe('default')
    await waitFor(() => expect(chatSlotAgent).toHaveBeenCalledWith('slot-a', 'kirocrew-conductor', 'template'))
    await act(async () => { land({ agent: 'kirocrew-conductor', workspace: 'default' }) })
    await waitFor(() => expect(store.getState().dashboard.slots[0].agent).toBe('kirocrew-conductor'))
    // Same element all the way through: one mount, so one entrance.
    expect(screen.getByTestId('crew-mode-welcome')).toBe(welcome)
  })

  it('gives the welcome up once the first message lands', async () => {
    const store = await renderWith({ messages: [] })
    fireEvent.click(screen.getByTestId('crew-mode-chip'))
    await screen.findByTestId('crew-mode-welcome')
    await act(async () => {
      store.dispatch(appendMessage({ role: 'user', content: 'plan it', ts: 1 } as unknown as Parameters<typeof appendMessage>[0]))
    })
    await waitFor(() => expect(screen.queryByTestId('crew-mode-welcome')).toBeNull())
  })

  it('brings the chips back when the switch is refused, as the chat never turned on', async () => {
    chatSlotAgent.mockImplementationOnce(async () => { throw new Error('refused') })
    await renderWith({ messages: [] })
    fireEvent.click(screen.getByTestId('crew-mode-chip'))
    await waitFor(() => expect(screen.getByTestId('crew-mode-chip')).toBeInTheDocument())
    expect(screen.getByTestId('memory-mode-chip')).toBeInTheDocument()
  })

  it('turns off from the mark before the first message, back to the agent it had', async () => {
    chatSlotAgent.mockClear()
    const store = await renderWith({ messages: [], agent: 'kirocrew-conductor', agentKind: 'template' })
    const mark = screen.getByRole('button', { name: 'Crew Mode', pressed: true })
    expect(mark).toHaveAttribute('title', 'Turn off Crew Mode. You can do this until you send your first message.')
    fireEvent.click(mark)
    // The chips come back at once, the mark goes, and the switch is the ordinary one.
    expect(screen.getByTestId('crew-mode-chip')).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByTestId('memory-mode-chip')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-mode-composer-chip')).toBeNull()
    await waitFor(() => expect(chatSlotAgent).toHaveBeenCalledWith('slot-a', 'default', 'template'))
    await waitFor(() => expect(store.getState().dashboard.slots[0].agent).toBe('default'))
    expect(screen.queryByTestId('crew-mode-welcome')).toBeNull()
  })

  it('offers no chip and no way off once a message is sent', async () => {
    chatSlotAgent.mockClear()
    await renderWith({ messages: [{ role: 'user', content: 'hello' }], agent: 'kirocrew-conductor', agentKind: 'template' })
    expect(screen.queryByTestId('crew-mode-chip')).toBeNull()
    expect(screen.queryByTestId('memory-mode-chip')).toBeNull()
    expect(screen.queryByTestId('crew-mode-note')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Crew Mode' })).toBeNull()
    expect(chatSlotAgent).not.toHaveBeenCalled()
  })

  it('has no note while the mode is off and no reply runs', async () => {
    await renderWith({ messages: [] })
    expect(screen.queryByTestId('crew-mode-note')).toBeNull()
    expect(screen.getByTestId('crew-mode-chip')).not.toHaveAttribute('aria-describedby')
  })

  it('is busy, still focusable, with a visible hint while the server reports a reply running', async () => {
    // A new chat's composer is idle (the welcome state needs it), but the listed
    // slot can still report a reply running on the server.
    const store = await renderWith({ messages: [], listedRunning: true })
    expect(store.getState().dashboard.slots[0].running).toBe(true)
    const chip = screen.getByTestId('crew-mode-chip')
    expect(chip).toHaveAttribute('aria-disabled', 'true')
    expect(chip).not.toBeDisabled()
    const note = screen.getByTestId('crew-mode-note')
    expect(note).toHaveTextContent('Available after the reply.')
    expect(chip).toHaveAttribute('aria-describedby', note.id)
    chatSlotAgent.mockClear()
    fireEvent.click(chip)
    expect(chatSlotAgent).not.toHaveBeenCalled()
    expect(screen.getByTestId('crew-mode-chip')).toBeInTheDocument()
  })

  it('is not offered on a remote-instance chat', async () => {
    await renderWith({ messages: [], instanceId: 'peer-1' })
    expect(screen.getByTestId('composer-memory-chip')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-mode-chip')).toBeNull()
  })

  it('is held back while the chat is still loading', async () => {
    // The detail read stays pending, so the page keeps its loading state: the chip
    // is not drawn alone, to slide across the memory chip arriving beside it.
    detail.hold = true
    await renderWith({ messages: [], slotLoading: true })
    expect(screen.queryByTestId('crew-mode-chip')).toBeNull()
  })

  it('opens the side panel task board when turned on in a root chat', async () => {
    const store = await renderWith({ messages: [] })
    expect(store.getState().chat.activityOpen).toBe(false)
    fireEvent.click(screen.getByTestId('crew-mode-chip'))
    await waitFor(() => expect(store.getState().chat.activityOpen).toBe(true))
    expect(await screen.findByTestId('command-center-panel', {}, { timeout: 5000 })).toBeInTheDocument()
  })

  it('turns on in a dispatched chat without opening a board it does not have', async () => {
    chatSlotAgent.mockClear()
    const store = await renderWith({ messages: [], createdBy: 'slot-parent' })
    fireEvent.click(screen.getByTestId('crew-mode-chip'))
    await waitFor(() => expect(chatSlotAgent).toHaveBeenCalledWith('slot-a', 'kirocrew-conductor', 'template'))
    expect(await screen.findByTestId('crew-mode-welcome')).toBeInTheDocument()
    expect(store.getState().chat.activityOpen).toBe(false)
    expect(screen.queryByTestId('crew-mode-open-board')).toBeNull()
  })

  it('opens the task board from the welcome page button', async () => {
    const store = await renderWith({ messages: [], agent: 'kirocrew-conductor', agentKind: 'template' })
    expect(screen.queryByTestId('crew-mode-sample-question')).toBeNull()
    fireEvent.click(screen.getByTestId('crew-mode-open-board'))
    await waitFor(() => expect(store.getState().chat.activityOpen).toBe(true))
  })

  it('is absent mid-chat while the mode is off', async () => {
    await renderWith({ messages: [{ role: 'user', content: 'hello' }] })
    expect(screen.queryByTestId('crew-mode-chip')).toBeNull()
    expect(screen.queryByTestId('crew-mode-note')).toBeNull()
  })

  it('is offered on the default template', async () => {
    await renderWith({ messages: [], agent: 'default', agentKind: 'template' })
    expect(screen.getByTestId('crew-mode-chip')).toBeInTheDocument()
  })

  it('is not offered in a crewmate chat, even one named like the default', async () => {
    // A plain session is the default template, never a crewmate: a crewmate's
    // chat keeps that crewmate.
    await renderWith({ messages: [], agent: 'default', agentKind: 'member' })
    expect(screen.queryByTestId('crew-mode-chip')).toBeNull()
  })

  it('is not offered on another agent or on a mode session', async () => {
    await renderWith({ messages: [], agent: 'custom-x' })
    expect(screen.queryByTestId('crew-mode-chip')).toBeNull()
  })

  it('is not offered on a crew member that happens to be named kirocrew-conductor', async () => {
    await renderWith({ messages: [], agent: 'kirocrew-conductor', agentKind: 'member' })
    expect(screen.queryByTestId('crew-mode-chip')).toBeNull()
  })

  it('offers no memory-mode switch on a Crew Mode chat, so it stays on the conductor', async () => {
    await renderWith({ messages: [], agent: 'kirocrew-conductor', agentKind: 'template' })
    expect(screen.queryByText('Choose memory mode')).toBeNull()
  })

  it('keeps a plain chat on its agent when it switches to Incognito', async () => {
    createChatSlot.mockClear()
    createChatSlot.mockResolvedValueOnce({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' })
    await renderWith({ messages: [], agent: 'default', agentKind: 'template' })
    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    // The pick crosses ChatPage's lazy `import('./chat/memoryModeSwitch')`.
    await waitFor(() => expect(createChatSlot).toHaveBeenCalled(), { timeout: 5000 })
    expect(createChatSlot.mock.calls[0][1]).toBe('default')
    expect(createChatSlot.mock.calls[0][8]).toBe('template')
  })

  it('is not offered on an Incognito or Temporary chat', async () => {
    await renderWith({ messages: [], memoryMode: 'incognito' })
    expect(screen.queryByTestId('crew-mode-chip')).toBeNull()
  })

  it('is not offered on an app-owned chat', async () => {
    await renderWith({ messages: [], origin: 'app' })
    expect(screen.queryByTestId('crew-mode-chip')).toBeNull()
  })

  it('is not offered when the conductor itself is the default agent', async () => {
    // Off would switch to the default agent, which is the conductor again.
    agentsState.defaultAgent = 'kirocrew-conductor'
    await renderWith({ messages: [], agent: 'kirocrew-conductor', agentKind: 'template' })
    expect(screen.queryByTestId('crew-mode-chip')).toBeNull()
  })

  it('is not offered on a legacy orchestrator-mode session', async () => {
    await renderWith({ messages: [], mode: 'orchestrator' })
    expect(screen.queryByTestId('crew-mode-chip')).toBeNull()
  })
})
