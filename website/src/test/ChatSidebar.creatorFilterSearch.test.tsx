/** A creator filter keeps the search field on screen, so the query it still sends stays visible and editable. */
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import ChatSidebar from '../pages/ChatSidebar'
import { ThemeProvider } from '../hooks/useTheme'
import { createTestStore } from './helpers'
import type { RootState } from '../store'

const SLOT = 'chat-1'

vi.mock('../pages/chat-sidebar/CreatorFilter', async importOriginal => ({
  ...(await importOriginal<typeof import('../pages/chat-sidebar/CreatorFilter')>()),
  useSharedCrewListView: () => ({ filterAvailable: true, filter: 'mine', setFilter: vi.fn() }),
  CreatorFilteredSessions: () => <div data-testid="creator-filtered-sessions" />,
}))

function mount() {
  const slots = [{ key: SLOT, title: 'A session', running: false, tags: [], created: '', last_ts: '' }]
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon',
      sessionColorsIntensity: 'clear',
    } as RootState['dashboard'],
    chat: { activeSlot: SLOT } as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={SLOT} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

describe('sidebar creator filter', () => {
  it('keeps the search field beside the filtered list', () => {
    mount()
    expect(screen.getByTestId('creator-filtered-sessions')).toBeInTheDocument()
    expect(screen.getByTestId('list-dock')).toBeInTheDocument()
    expect(screen.getByTestId('search-field-glass')).toBeInTheDocument()
  })
})
