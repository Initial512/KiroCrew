/**
 * The worker breadcrumb on a phone: the root's name keeps a readable minimum and
 * a tooltip, the worker's name stays, the status is a dot with its word for
 * readers, and Back keeps its 44x44 target. jsdom has no layout, so the rules are
 * pinned on the classes that carry them; the pod check measures the pixels.
 */
import { describe, it, expect, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../test/helpers'

const mobile = vi.hoisted(() => ({ value: true }))
vi.mock('../../hooks/useIsMobile', () => ({ useIsMobile: () => mobile.value }))
const fail = vi.hoisted(() => ({ on: false, workers: [] as unknown[] }))
vi.mock('../../api/client', () => ({ api: { crewWorkers: () => (fail.on ? Promise.reject(new Error('down')) : Promise.resolve({ root: 'root', workers: fail.workers })) } }))

import { CrewWorkerBreadcrumb } from './CrewWorkerBreadcrumb'

function renderCrumb() {
  const store = createTestStore({ dashboard: { slots: [{ key: 'root', title: 'New Teammate Welcome Kit', crew_mode_root: true }] } } as never)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}><Provider store={store}>
      <CrewWorkerBreadcrumb root="root" worker={{ key: 'w1', title: 'First-week checklist', running: false }} />
    </Provider></QueryClientProvider>,
  )
}

describe('CrewWorkerBreadcrumb on a phone', () => {
  it('keeps the root readable, names both, and keeps Back a 44px target', () => {
    renderCrumb()
    const rootCrumb = screen.getByTestId('crew-breadcrumb-root')
    expect(rootCrumb).toHaveClass('min-w-[5.5rem]')
    expect(rootCrumb).toHaveAttribute('title', 'New Teammate Welcome Kit')
    const worker = screen.getByTestId('crew-breadcrumb-worker')
    expect(worker).toHaveTextContent('First-week checklist')
    expect(worker).toHaveAttribute('title', 'First-week checklist')
    const back = screen.getByTestId('crew-back-to-conductor')
    expect(back).toHaveClass('min-h-11', 'min-w-11', 'md:min-h-8', 'md:min-w-0')
    // The status word is for readers only on a phone; the dot carries it.
    expect(screen.getByTestId('crew-status').querySelector('.sr-only')).not.toBeNull()
  })

  it('shows the status word on a desktop header', () => {
    mobile.value = false
    renderCrumb()
    expect(screen.getByTestId('crew-status').querySelector('.sr-only')).toBeNull()
    mobile.value = true
  })

  it('says a failed worker-list read beside the status it fell back to', async () => {
    fail.on = true
    try {
      renderCrumb()
      expect(await screen.findByText('Could not load the workers.')).toBeInTheDocument()
      expect(screen.getByTestId('crew-worker-breadcrumb')).toBeInTheDocument()
    } finally {
      fail.on = false
    }
  })

  it('keeps the last group read when a later poll fails: a done worker stays Done', async () => {
    // The list answered once (the worker is done); a 5s poll then fails. The
    // row's own flags cannot say done, so the last read stands under the notice.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    fail.workers = [{ key: 'w1', title: 'First-week checklist', agent: 'kirocrew-worker', status: 'idle', group: 'done', last_activity: '', ledger_item: '', summary: '', created_by: '' }]
    try {
      renderCrumb()
      expect(await screen.findByText('Done')).toBeInTheDocument()
      fail.on = true
      await act(async () => { vi.advanceTimersByTime(16_000) })
      expect(await screen.findByText('Could not load the workers.')).toBeInTheDocument()
      expect(screen.getByTestId('crew-status')).toHaveAttribute('data-group', 'done')
    } finally {
      vi.useRealTimers()
      fail.on = false
      fail.workers = []
    }
  })
})
