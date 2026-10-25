import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import type { TailscaleInvite, TailscaleInvitesStatus } from '../../api/sharedCrewInvites'
import { parseEmails, looksLikeApiKey } from '../../api/sharedCrewInvites'
import { TailscaleInvites, inviteErrorText } from './TailscaleInvites'

vi.mock('../../api/client', () => ({
  api: {
    tailscaleInvites: vi.fn(),
    tailscaleConnect: vi.fn(),
    tailscaleDisconnect: vi.fn(),
    tailscaleCreateInvites: vi.fn(),
    tailscaleResendInvite: vi.fn(),
    tailscaleRevokeInvite: vi.fn(),
    tailscaleUploadAvatar: vi.fn(),
    sharedCrewAccess: vi.fn(),
    sharedCrewAckFirstShare: vi.fn(),
  },
}))
const read = vi.mocked(api.tailscaleInvites)
const connect = vi.mocked(api.tailscaleConnect)
const create = vi.mocked(api.tailscaleCreateInvites)
const revoke = vi.mocked(api.tailscaleRevokeInvite)
const disconnect = vi.mocked(api.tailscaleDisconnect)
const accessRead = vi.mocked(api.sharedCrewAccess)
const ack = vi.mocked(api.sharedCrewAckFirstShare)

const KEY = 'tskey-api-kAbC123CNTRL-Zz9yYxXwWvV'

function status(over: Partial<TailscaleInvitesStatus> = {}): TailscaleInvitesStatus {
  return {
    connected: true, tailnet: 'example.ts.net', key_expires: '2099-01-01T00:00:00Z', key_expired: false, key_expires_soon: false,
    admin_share_url: 'https://login.tailscale.com/admin/machines', admin_keys_url: 'https://login.tailscale.com/admin/settings/keys',
    invites: [], members: [], last_sync_at: 0, sync: {}, ...over,
  }
}

function invite(over: Partial<TailscaleInvite> = {}): TailscaleInvite {
  return { id: '101', kind: 'email', email: 'ann@example.com', multi_use: false, alias: '', avatar_id: '', created_at: 1_700_000_000, status: 'pending', accepted_login: '', ...over }
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><TailscaleInvites /></QueryClientProvider>)
}

beforeEach(() => { vi.clearAllMocks(); accessRead.mockResolvedValue(null) })

describe('TailscaleInvites', () => {
  it('without a key: shows the admin-console share path and the one-time key form', async () => {
    read.mockResolvedValue(status({ connected: false }))
    mount()
    expect(await screen.findByTestId('invite-connect')).toBeInTheDocument()
    expect(screen.getByTestId('invite-admin-share')).toHaveAttribute('href', 'https://login.tailscale.com/admin/machines')
    expect(screen.getByTestId('invite-admin-keys')).toHaveAttribute('href', 'https://login.tailscale.com/admin/settings/keys')
    const input = screen.getByTestId('invite-key-input')
    expect(input).toHaveAttribute('type', 'password')
    expect(screen.getByTestId('invite-key-connect')).toBeDisabled()
    fireEvent.change(input, { target: { value: 'tskey-client-abc-def' } })
    expect(screen.getByTestId('invite-key-connect')).toBeDisabled()
  })

  it('connects once and clears the key from the field', async () => {
    read.mockResolvedValue(status({ connected: false }))
    connect.mockResolvedValue(status())
    mount()
    const input = await screen.findByTestId('invite-key-input')
    fireEvent.change(input, { target: { value: KEY } })
    fireEvent.click(screen.getByTestId('invite-key-connect'))
    await waitFor(() => expect(connect).toHaveBeenCalledWith(KEY))
    expect(await screen.findByTestId('invite-connected')).toBeInTheDocument()
    expect(screen.getByText('Connected to example.ts.net')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('Zz9yYxXwWvV')
  })

  it('names a refused key without a sign-out', async () => {
    read.mockResolvedValue(status({ connected: false }))
    connect.mockRejectedValue(new ApiError(502, 'x', JSON.stringify({ code: 'tailscale_auth_failed' })))
    mount()
    fireEvent.change(await screen.findByTestId('invite-key-input'), { target: { value: KEY } })
    fireEvent.click(screen.getByTestId('invite-key-connect'))
    expect(await screen.findByTestId('invite-key-error')).toHaveTextContent('Tailscale did not accept this key')
  })

  it('sends email invites with the alias, single-use, never an exit-node option', async () => {
    read.mockResolvedValue(status())
    create.mockResolvedValue({ invites: [invite()] })
    mount()
    fireEvent.change(await screen.findByTestId('invite-emails'), { target: { value: 'ann@example.com, bo@example.com' } })
    fireEvent.change(screen.getByTestId('invite-alias'), { target: { value: 'Ann' } })
    fireEvent.click(screen.getByTestId('invite-send'))
    await waitFor(() => expect(create).toHaveBeenCalledWith({ emails: ['ann@example.com', 'bo@example.com'], multi_use: false, alias: 'Ann', avatar_id: '' }))
    expect(screen.queryByText(/exit node/i)).toBeNull()
  })

  it('blocks a malformed email before sending', async () => {
    read.mockResolvedValue(status())
    mount()
    fireEvent.change(await screen.findByTestId('invite-emails'), { target: { value: 'not-an-email' } })
    expect(screen.getByTestId('invite-email-error')).toBeInTheDocument()
    expect(screen.getByTestId('invite-send')).toBeDisabled()
  })

  it('makes a link invite and offers to copy it with a warning', async () => {
    read.mockResolvedValue(status())
    create.mockResolvedValue({ invites: [invite({ kind: 'link', email: '', invite_url: 'https://login.tailscale.com/admin/invite/abc' })] })
    mount()
    fireEvent.click(await screen.findByTestId('invite-mode-link'))
    fireEvent.click(screen.getByTestId('invite-send'))
    await waitFor(() => expect(create).toHaveBeenCalledWith({ emails: [], multi_use: false, alias: '', avatar_id: '' }))
    expect(await screen.findByTestId('invite-link-result')).toHaveTextContent('https://login.tailscale.com/admin/invite/abc')
    expect(screen.getByText(/Share it like a password/)).toBeInTheDocument()
  })

  it('lists pending and accepted invites, naming an accept from another account', async () => {
    read.mockResolvedValue(status({
      invites: [
        invite({ id: '1', status: 'pending' }),
        invite({ id: '2', email: 'bo@work.example', status: 'accepted', accepted_login: 'bo@gmail.example', alias: 'Bo' }),
      ],
    }))
    mount()
    expect(await screen.findByTestId('invite-1')).toHaveAttribute('data-status', 'pending')
    expect(screen.getByTestId('invite-2')).toHaveAttribute('data-status', 'accepted')
    expect(screen.getByText('Bo')).toBeInTheDocument()
    expect(screen.getByText('bo@gmail.example')).toBeInTheDocument()
    expect(screen.getByTestId('invite-other-account')).toHaveTextContent('bo@work.example')
    expect(screen.getAllByTestId('invite-resend')).toHaveLength(1)
  })

  it('revokes only after a confirm step', async () => {
    read.mockResolvedValue(status({ invites: [invite({ id: '9', status: 'accepted', accepted_login: 'ann@example.com' })] }))
    revoke.mockResolvedValue({ revoked: '9', member_removed: true })
    mount()
    fireEvent.click(await screen.findByTestId('invite-revoke'))
    expect(revoke).not.toHaveBeenCalled()
    expect(screen.getByText(/closes their open Crew tabs/)).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('invite-revoke-confirm'))
    await waitFor(() => expect(revoke).toHaveBeenCalledWith('9'))
  })

  it('a pending email invite shows only Confirm and Cancel while confirming a revoke', async () => {
    read.mockResolvedValue(status({ invites: [invite({ id: '7' })] }))
    mount()
    expect(await screen.findByTestId('invite-resend')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('invite-revoke'))
    expect(screen.getByTestId('invite-revoke-confirm')).toBeInTheDocument()
    expect(screen.queryByTestId('invite-resend')).toBeNull()
  })

  it('removes the API key only after a confirm, since it cannot be undone', async () => {
    read.mockResolvedValue(status())
    disconnect.mockResolvedValue(status({ connected: false }))
    mount()
    fireEvent.click(await screen.findByTestId('invite-disconnect'))
    expect(disconnect).not.toHaveBeenCalled()
    expect(await screen.findByText(/can't be undone/)).toBeInTheDocument()
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Remove key' }))
    await waitFor(() => expect(disconnect).toHaveBeenCalledTimes(1))
  })

  it('shows a failed key removal', async () => {
    read.mockResolvedValue(status())
    disconnect.mockRejectedValue(new ApiError(500, 'x', JSON.stringify({ code: 'error' })))
    mount()
    fireEvent.click(await screen.findByTestId('invite-disconnect'))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Remove key' }))
    expect(await screen.findByTestId('invite-disconnect-error')).toBeInTheDocument()
  })

  it('asks the first-share question before the first invite, then sends', async () => {
    read.mockResolvedValue(status())
    accessRead.mockResolvedValue({
      readable: true, waiting: [], approved: [], seeded: [], first_share_ack: false,
      first_share: { needs_ack: true, sessions: 4, memory_items: 12, host: { state: 'available', reason: '', backend: 'caffeinate', laptop: false, laptop_reason: 'no_battery' } },
    })
    ack.mockResolvedValue(undefined)
    create.mockResolvedValue({ invites: [invite()] })
    mount()
    fireEvent.change(await screen.findByTestId('invite-emails'), { target: { value: 'ann@example.com' } })
    fireEvent.click(screen.getByTestId('invite-send'))
    expect(await screen.findByText('They will see every session (4 now) and Crew memory (12 items).')).toBeInTheDocument()
    expect(create).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Share it' }))
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1))
    expect(ack).toHaveBeenCalledTimes(1)
  })

  it('warns before the key expires and after it has', async () => {
    read.mockResolvedValue(status({ key_expires_soon: true }))
    const view = mount()
    expect(await screen.findByText('Key expires soon')).toBeInTheDocument()
    view.unmount()
    read.mockResolvedValue(status({ key_expired: true }))
    mount()
    expect(await screen.findByText('Key expired')).toBeInTheDocument()
  })

  it('renders nothing for a non-owner or a gateway without the routes', async () => {
    read.mockResolvedValue(null)
    const view = mount()
    await waitFor(() => expect(read).toHaveBeenCalled())
    expect(screen.queryByTestId('tailscale-invites')).toBeNull()
    view.unmount()
    read.mockRejectedValue(new ApiError(403, 'x', JSON.stringify({ code: 'owner_only' })))
    mount()
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
    expect(screen.queryByTestId('tailscale-invites')).toBeNull()
  })

  it('a failed read shows an error notice instead of an empty space', async () => {
    read.mockRejectedValue(new ApiError(500, 'x', JSON.stringify({ code: 'tailscale_auth_failed' })))
    mount()
    expect(await screen.findByTestId('invite-load-error')).toHaveTextContent('Tailscale did not accept this key')
    expect(screen.queryByTestId('tailscale-invites')).toBeNull()
  })
})

describe('invite helpers', () => {
  it('parses a pasted address list', () => {
    expect(parseEmails('a@x.io, b@x.io\nA@x.io;  c@x.io')).toEqual(['a@x.io', 'b@x.io', 'c@x.io'])
  })
  it('accepts only API access keys', () => {
    expect(looksLikeApiKey(KEY)).toBe(true)
    expect(looksLikeApiKey('tskey-auth-abc-def')).toBe(false)
  })
  it('has a sentence for every refusal code and a fallback', () => {
    expect(inviteErrorText('tailscale_rate_limited')).toMatch(/slow down/)
    expect(inviteErrorText('something_new')).toBe('That did not work. Try again.')
  })
})
