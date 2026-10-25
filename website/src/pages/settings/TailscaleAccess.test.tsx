import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import type { AccessView, FirstShare, WaitingPerson } from '../../api/sharedCrewAccess'
import { safeAvatarUrl } from '../../api/sharedCrewAccess'
import { TailscaleAccess } from './TailscaleAccess'

vi.mock('../../api/client', () => ({
  api: {
    sharedCrewAccess: vi.fn(),
    sharedCrewAckFirstShare: vi.fn(),
    sharedCrewApprove: vi.fn(),
    sharedCrewDismiss: vi.fn(),
    sharedCrewRemoveApproved: vi.fn(),
  },
}))
const read = vi.mocked(api.sharedCrewAccess)
const ack = vi.mocked(api.sharedCrewAckFirstShare)
const approve = vi.mocked(api.sharedCrewApprove)
const dismiss = vi.mocked(api.sharedCrewDismiss)
const remove = vi.mocked(api.sharedCrewRemoveApproved)

function share(over: Partial<FirstShare> = {}): FirstShare {
  return {
    needs_ack: false,
    sessions: 7,
    memory_items: 31,
    host: { state: 'available', reason: '', backend: 'caffeinate', laptop: false, laptop_reason: 'no_battery' },
    ...over,
  }
}

function waiting(over: Partial<WaitingPerson> = {}): WaitingPerson {
  return {
    user_id: '31', login: 'mallory@example.com', display_name: 'Mallory', avatar_url: '', node: 'mallory',
    first_seen: 1_760_000_000, last_seen: 1_760_000_000, attempts: 1, ...over,
  }
}

function view(over: Partial<AccessView> = {}): AccessView {
  return { readable: true, waiting: [], approved: [], seeded: [], first_share_ack: false, first_share: share(), ...over }
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><TailscaleAccess /></QueryClientProvider>)
}

beforeEach(() => { vi.clearAllMocks() })

describe('TailscaleAccess', () => {
  it('lists a waiting person with name, login, first sighting and Approve / Dismiss', async () => {
    read.mockResolvedValue(view({ waiting: [waiting({ avatar_url: 'https://pics.example/m.png' })] }))
    mount()
    const row = await screen.findByTestId('waiting-31')
    expect(within(row).getByText('Mallory')).toBeInTheDocument()
    expect(within(row).getByText('mallory@example.com')).toBeInTheDocument()
    expect(within(row).getByText(/First tried/)).toBeInTheDocument()
    const img = row.querySelector('img')
    expect(img).toHaveAttribute('src', 'https://pics.example/m.png')
    expect(img).toHaveAttribute('referrerpolicy', 'no-referrer')
    expect(within(row).getByTestId('waiting-approve')).toBeInTheDocument()
    expect(within(row).getByTestId('waiting-dismiss')).toBeInTheDocument()
  })

  it('approves straight away once someone else already has access', async () => {
    read.mockResolvedValue(view({ waiting: [waiting()], first_share: share({ needs_ack: false }) }))
    approve.mockResolvedValue({ approved: { user_id: '31', login: 'mallory@example.com', display_name: 'Mallory', avatar_url: '', first_seen: 0, approved_at: 1, approved_by: 'o' } })
    mount()
    fireEvent.click(await screen.findByTestId('waiting-approve'))
    await waitFor(() => expect(approve).toHaveBeenCalledWith('31'))
    expect(ack).not.toHaveBeenCalled()
  })

  it('shows the first-share warning with real counts and a laptop note, then approves', async () => {
    read.mockResolvedValue(view({
      waiting: [waiting()],
      first_share: share({ needs_ack: true, host: { state: 'available', reason: '', backend: 'caffeinate', laptop: true, laptop_reason: 'battery_present' } }),
    }))
    ack.mockResolvedValue(undefined)
    approve.mockResolvedValue({ approved: { user_id: '31', login: 'mallory@example.com', display_name: 'Mallory', avatar_url: '', first_seen: 0, approved_at: 1, approved_by: 'o' } })
    mount()
    fireEvent.click(await screen.findByTestId('waiting-approve'))
    expect(await screen.findByText('They will see every session (7 now) and Crew memory (31 items).')).toBeInTheDocument()
    expect(screen.getByTestId('first-share-laptop')).toHaveTextContent('Crew keeps it awake')
    expect(approve).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Share it' }))
    await waitFor(() => expect(approve).toHaveBeenCalledWith('31'))
    expect(ack).toHaveBeenCalledTimes(1)
  })

  it('cancelling the first-share warning approves nobody', async () => {
    read.mockResolvedValue(view({ waiting: [waiting()], first_share: share({ needs_ack: true }) }))
    mount()
    fireEvent.click(await screen.findByTestId('waiting-approve'))
    await screen.findByTestId('first-share-body')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByTestId('first-share-body')).toBeNull())
    expect(approve).not.toHaveBeenCalled()
    expect(ack).not.toHaveBeenCalled()
  })

  it('dismisses a waiting person', async () => {
    read.mockResolvedValue(view({ waiting: [waiting()] }))
    dismiss.mockResolvedValue(undefined)
    mount()
    fireEvent.click(await screen.findByTestId('waiting-dismiss'))
    await waitFor(() => expect(dismiss).toHaveBeenCalledWith('31'))
  })

  it('removes an approved person only after a confirm that says their turn stops', async () => {
    read.mockResolvedValue(view({
      approved: [{ user_id: '21', login: 'alice@example.com', display_name: 'Alice', avatar_url: '', first_seen: 0, approved_at: 1_760_000_000, approved_by: 'o' }],
    }))
    remove.mockResolvedValue({ removed: '21', stopped_running: 1, cancelled_queued: 0, closed_connections: 1 })
    mount()
    fireEvent.click(await screen.findByTestId('approved-remove'))
    expect(await screen.findByText(/a running turn stops now/)).toBeInTheDocument()
    expect(remove).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Remove access' }))
    await waitFor(() => expect(remove).toHaveBeenCalledWith('21'))
  })

  it('names the empty list, the migrated entries and an unreadable list', async () => {
    read.mockResolvedValue(view({ readable: false, seeded: ['alice@example.com', 'tag:ci'] }))
    mount()
    expect(await screen.findByTestId('waiting-empty')).toBeInTheDocument()
    expect(screen.getByTestId('access-seeded')).toHaveTextContent('alice@example.com')
    expect(screen.getByTestId('access-unreadable')).toBeInTheDocument()
  })

  it('a failed approve shows an error notice with a hand-off', async () => {
    read.mockResolvedValue(view({ waiting: [waiting()] }))
    approve.mockRejectedValue(new ApiError(409, 'x', JSON.stringify({ code: 'not_waiting' })))
    mount()
    fireEvent.click(await screen.findByTestId('waiting-approve'))
    const notice = await screen.findByTestId('waiting-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(within(notice).getByRole('button', { name: /Ask the agent/ })).toBeInTheDocument()
  })

  it('a failed removal shows an error notice with a hand-off', async () => {
    read.mockResolvedValue(view({
      approved: [{ user_id: '21', login: 'alice@example.com', display_name: 'Alice', avatar_url: '', first_seen: 0, approved_at: 1_760_000_000, approved_by: 'o' }],
    }))
    remove.mockRejectedValue(new ApiError(500, 'x', JSON.stringify({ code: 'error' })))
    mount()
    fireEvent.click(await screen.findByTestId('approved-remove'))
    fireEvent.click(await screen.findByRole('button', { name: 'Remove access' }))
    const notice = await screen.findByTestId('approved-remove-error')
    expect(within(notice).getByRole('button', { name: /Ask the agent/ })).toBeInTheDocument()
  })

  it('renders nothing for a non-owner', async () => {
    read.mockRejectedValue(new ApiError(403, 'x', JSON.stringify({ code: 'owner_only' })))
    mount()
    await waitFor(() => expect(read).toHaveBeenCalled())
    expect(screen.queryByTestId('tailscale-access')).toBeNull()
  })
})

describe('safeAvatarUrl', () => {
  it('keeps only plain https pictures', () => {
    expect(safeAvatarUrl('https://pics.example/a.png')).toBe('https://pics.example/a.png')
    expect(safeAvatarUrl('http://pics.example/a.png')).toBe('')
    expect(safeAvatarUrl('https://user:pw@pics.example/a.png')).toBe('')
    expect(safeAvatarUrl('javascript:alert(1)')).toBe('')
  })
})
