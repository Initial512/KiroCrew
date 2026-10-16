// MCP Management: one press shares a server.
//
// Contract under test:
// - every row action is a real labelled <button> ("Share with all my chats", "Stop
//   sharing", "Share updated version"), never a bare switch
// - sharing is ONE request: no review dialog (the endpoint records the exact
//   launch itself)
// - a share never turns page-wide sharing on; the switch asks before it does
// - a shared row whose command changed reads "Paused · command changed", and its
//   confirm button pins the refused launch's own identity
// - the page shows no launch command anywhere: one press reads the launch and
//   writes the share pinned to it; Share all lists servers by name and shares
//   only on a second press, pinned to the launch read for each
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { McpManagement } from '../pages/settings/McpManagement'

function server(over: Record<string, unknown> = {}) {
  return {
    name: 'alpha-mcp',
    stub: false,
    can_stub: true,
    in_allowlist: false,
    entry_poolable: false,
    agents: ['kirocrew'],
    transport: 'stdio',
    denylisted: false,
    ...over,
  }
}

function launch(over: Record<string, unknown> = {}) {
  return {
    name: 'alpha-mcp',
    commands: [['/opt/alpha', '--one']],
    envs: [[]],
    complete: true,
    expected_launch: 'alpha-identity',
    ...over,
  }
}

const status = (over: Record<string, unknown> = {}) => ({
  enabled: false,
  stub: [] as string[],
  stub_count: 0,
  running: false,
  ping_ok: false,
  supported: true,
  ...over,
})

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <McpManagement />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
})
afterEach(cleanup)

describe('one-step sharing', () => {
  it('shares on one press, with no review dialog', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch() as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({
      ok: true, name: 'alpha-mcp', stub: true, restart_required: true,
    } as never)

    const { container } = mount()
    const share = await screen.findByRole('button', { name: 'Share with all my chats alpha-mcp' })
    expect(share.tagName).toBe('BUTTON')
    expect(share.textContent).toContain('Share with all my chats')
    expect(screen.queryByRole('switch', { name: 'Share with all my chats alpha-mcp' })).toBeNull()
    // Nothing is read before the press, and no command is on the page.
    expect(api.mcpGatewayLaunchPreview).not.toHaveBeenCalled()
    expect((share as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(share)

    // Chain: the press -> the launch read -> one stub POST pinned to it.
    await waitFor(() => expect(setStub).toHaveBeenCalledTimes(1), { timeout: 5000 })
    expect(api.mcpGatewayLaunchPreview).toHaveBeenCalledWith('alpha-mcp')
    // The launch approved is exactly the one read for this press.
    expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'alpha-identity')
    expect(container.textContent).not.toContain('/opt/alpha')
    // Nothing asks a second time.
    expect(screen.queryByRole('button', { name: /approve/i })).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('says the press only marks the server while page-wide sharing is off', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)

    mount()
    const share = await screen.findByRole('button', { name: 'Mark to share alpha-mcp' })
    await waitFor(() => expect(share.textContent).toContain('Mark to share'), { timeout: 5000 })
    expect(share.textContent).not.toContain('Share with all my chats')
  })

  it('keeps the confirm closed when the marked list cannot be read back', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false }) as never)
    vi.spyOn(api, 'mcpGatewayServers')
      .mockResolvedValueOnce({ servers: [server()] } as never)
      .mockRejectedValue(new Error('read failed'))
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch() as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)

    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Mark to share alpha-mcp' }))
    await waitFor(() => expect(setStub).toHaveBeenCalledTimes(1), { timeout: 5000 })
    // Chain: the stub POST -> the read-back fails -> no dialog.
    expect(await screen.findByText('Could not load the server list. This is not the same as having none configured.', undefined, { timeout: 5000 })).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('marks the server, then asks before turning page-wide sharing on', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)
    const enable = vi.spyOn(api, 'mcpGatewayEnable').mockResolvedValue({ ping_ok: true } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch() as never)

    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Mark to share alpha-mcp' }))
    // Chain: the press -> the launch read -> the stub POST -> the switch's own confirm.
    await waitFor(() => expect(setStub).toHaveBeenCalledTimes(1), { timeout: 5000 })
    const dialog = await screen.findByRole('dialog')
    expect(enable).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Turn on sharing' }))
    await waitFor(() => expect(enable).toHaveBeenCalledWith(true))
  })

  it('stops sharing with its own button', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: false } as never)

    mount()
    const stop = await screen.findByRole('button', { name: 'Stop sharing alpha-mcp' })
    expect(stop.textContent).toContain('Stop sharing')
    fireEvent.click(stop)

    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', false, undefined))
  })

  it('confirms a changed command pinned to the refused launch, showing no command', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        enabled: true,
        stub: ['alpha-mcp'],
        launch_refused: {
          'alpha-mcp': {
            reason: 'changed_needs_reapproval',
            commands: [['/opt/alpha', '--two']],
            envs: [['MODE=new']],
            approved_commands: [['/opt/alpha', '--one']],
            approved_envs: [['MODE=old']],
            complete: true,
            expected_launch: 'pinned-identity',
          },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    const preview = vi.spyOn(api, 'mcpGatewayLaunchPreview')
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)

    const { container } = mount()
    const confirm = await screen.findByRole('button', { name: 'Share updated version alpha-mcp' })
    expect(confirm.textContent).toContain('Share updated version')
    expect(screen.getAllByText('Paused · command changed').length).toBeGreaterThan(0)
    expect(screen.getByText('Its command or version changed since you shared it. Each chat runs its own copy until you share the updated version.')).toBeTruthy()
    // Neither the new command nor the one approved before is on the page.
    for (const text of ['/opt/alpha', 'MODE=new', 'MODE=old']) {
      expect(container.textContent).not.toContain(text)
    }
    fireEvent.click(confirm)

    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'pinned-identity'))
    expect(setStub).toHaveBeenCalledTimes(1)
    // The refusal carries the identity; nothing is read again to confirm it.
    expect(preview).not.toHaveBeenCalled()
  })

  it('names a server never approved here as such, not as a changed command', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        enabled: true,
        stub: ['alpha-mcp'],
        launch_refused: {
          'alpha-mcp': { reason: 'added_outside_dashboard', complete: true, expected_launch: 'first-identity' },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)

    mount()
    const share = await screen.findByRole('button', { name: 'Share with all my chats alpha-mcp' })
    expect(share.textContent).toContain('Share with all my chats')
    expect(screen.getAllByText('Paused · not approved yet').length).toBeGreaterThan(0)
    expect(screen.getByText('Declared in a config file, and never approved here.')).toBeTruthy()
    expect(screen.queryByText('Paused · command changed')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Share updated version alpha-mcp' })).toBeNull()
    // Its share stands alone: no opposite choice beside it.
    expect(screen.queryByRole('button', { name: 'Stop sharing alpha-mcp' })).toBeNull()
    fireEvent.click(share)

    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'first-identity'))
  })

  it('counts every row it lists in the header, a blocked one too', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({ stub: true, in_allowlist: true }),
        server({ name: 'beta-mcp' }),
        server({ name: 'gamma-mcp', can_stub: false }),
      ],
    } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch() as never)

    mount()
    expect(await screen.findByText('1 of 3 shared')).toBeTruthy()
  })

  it('reads a marked row as paused, and does not count it, while sharing is off', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true }), server({ name: 'beta-mcp' })],
    } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch() as never)

    mount()
    expect(await screen.findByText('0 of 2 shared')).toBeTruthy()
    expect(screen.getAllByText('Marked · waiting for Sharing').length).toBeGreaterThan(0)
  })

  it('offers no confirm for a refused launch with no approvable identity, and keeps a way to turn it off', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        enabled: true,
        stub: ['alpha-mcp'],
        launch_refused: { 'alpha-mcp': { reason: 'changed_needs_reapproval', complete: false } },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    expect(
      await screen.findByText("This server's command cannot be approved for sharing, so it keeps running inside each session."),
    ).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Share updated version alpha-mcp' })).toBeNull()
    expect(setStub).not.toHaveBeenCalled()
    // Its chip says it cannot be shared, not that a fixable change is waiting.
    expect(screen.getAllByText("Can't share").length).toBeGreaterThan(0)
    expect(screen.queryByText('Paused · command changed')).toBeNull()
    // It can still be turned off.
    const stop = screen.getByRole('button', { name: 'Remove mark from alpha-mcp' })
    expect((stop as HTMLButtonElement).disabled).toBe(false)
  })
})

describe('accessible names', () => {
  it.each([true, false])('start with the visible label, so a voice command for it matches (sharing on: %s)', async on => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: on }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)

    mount()
    const label = on ? 'Share with all my chats' : 'Mark to share'
    const button = await screen.findByRole('button', { name: `${label} alpha-mcp` })
    expect(button.textContent).toContain(label)
  })
})

describe('the page-wide switch', () => {
  it('names the sharing-on-with-nothing-marked state rather than showing a live switch', async () => {
    // Reachable by unmarking the last server: the switch stays on over an empty
    // set, a switch with no observable effect.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)

    mount()
    expect(await screen.findByText('Sharing is on, but no server is marked yet. Mark one above.')).toBeTruthy()
  })

  it('lists only the servers it will share, and lets a warned one be left out', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        enabled: false,
        stub: ['alpha-mcp', 'beta-mcp'],
        launch_refused: { 'beta-mcp': { reason: 'added_outside_dashboard', complete: true, expected_launch: 'b' } },
      }) as never,
    )
    const refuted = { strength: 'refuted', recommendStub: false, recommendShare: false, reasons: [] }
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({ stub: true, in_allowlist: true, recommendation: refuted }),
        server({ name: 'beta-mcp', stub: true, in_allowlist: true }),
      ],
    } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: false } as never)

    mount()
    const sharing = await screen.findByRole('switch', { name: 'Sharing for marked servers' })
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(sharing)
    const dialog = await screen.findByRole('dialog')
    expect(dialog.textContent).toContain('Marked now: 1.')
    expect(dialog.textContent).not.toContain('beta-mcp')
    fireEvent.click(within(dialog).getByRole('button', { name: "Don't share alpha-mcp" }))
    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', false, undefined))
  })

  it('words a paused changed row as marked, not shared, while sharing is off', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        enabled: false,
        stub: ['alpha-mcp'],
        launch_refused: {
          'alpha-mcp': {
            reason: 'changed_needs_reapproval',
            complete: true,
            expected_launch: 'x',
            commands: [['/opt/a', '--two']],
            approved_commands: [['/opt/a', '--one']],
          },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server({ stub: true, in_allowlist: true })] } as never)

    mount()
    const button = await screen.findByRole('button', { name: 'Mark updated version alpha-mcp' })
    expect(button.textContent).toContain('Mark updated version')
    expect(screen.getByText(/^Its command or version changed since you marked it\./)).toBeTruthy()
  })

  it.each([
    [{ commands: [['/opt/a', '--two']], approved_commands: [['/opt/a', '--one']] }, 'Its command or version changed since you shared it.'],
    [
      { commands: [['/opt/a']], approved_commands: [['/opt/a']], envs: [['MODE=new']], approved_envs: [['MODE=old']] },
      'Its settings changed since you shared it.',
    ],
  ])('says what kind of change paused a server, without showing it', async (change, line) => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        enabled: true,
        stub: ['alpha-mcp'],
        launch_refused: {
          'alpha-mcp': { reason: 'changed_needs_reapproval', complete: true, expected_launch: 'x', ...change },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server({ stub: true, in_allowlist: true })] } as never)

    const { container } = mount()
    expect(await screen.findByText(text => text.startsWith(line))).toBeTruthy()
    expect(container.textContent).not.toContain('/opt/a')
    expect(container.textContent).not.toContain('MODE=')
  })

  it('names the marked servers in its confirm', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true }), server({ name: 'beta-mcp' })],
    } as never)

    mount()
    const sharing = await screen.findByRole('switch', { name: 'Sharing for marked servers' })
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(sharing)
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('alpha-mcp')).toBeTruthy()
    expect(within(dialog).queryByText('beta-mcp')).toBeNull()
  })

  it("offers Don't share, not Stop sharing, on a marked row its env keeps unshared", async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true, pooling_blocked_by_env: true })],
    } as never)

    mount()
    expect(await screen.findByRole('button', { name: "Don't share alpha-mcp" })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Stop sharing alpha-mcp' })).toBeNull()
  })

  it("offers Don't share, not Stop sharing, on a marked row while sharing is off", async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: false } as never)

    mount()
    const unmark = await screen.findByRole('button', { name: "Don't share alpha-mcp" })
    expect(unmark.textContent).toContain("Don't share")
    expect(screen.queryByRole('button', { name: 'Stop sharing alpha-mcp' })).toBeNull()
    fireEvent.click(unmark)
    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', false, undefined))
  })

  it('asks before it turns sharing on, and turns it on only on the confirm', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    const enable = vi.spyOn(api, 'mcpGatewayEnable').mockResolvedValue({ ping_ok: true } as never)

    mount()
    const sharing = await screen.findByRole('switch', { name: 'Sharing for marked servers' })
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(sharing)
    const dialog = await screen.findByRole('dialog')
    expect(enable).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Turn on sharing' }))

    await waitFor(() => expect(enable).toHaveBeenCalledWith(true))
  })


  it('names marked servers the check argues against while sharing is off', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false, stub: ['beta-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({ name: 'beta-mcp', stub: true, in_allowlist: true, recommendation: { strength: 'refuted', recommendStub: false, recommendShare: false, reasons: [] } }),
      ],
    } as never)

    mount()
    expect(await screen.findByText(/the check advises against: beta-mcp$/)).toBeTruthy()
  })
})

describe('the launch a share approves', () => {
  it('turns nothing on when the launch read at the press cannot be approved', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch({ complete: false, expected_launch: undefined }) as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Share with all my chats alpha-mcp' }))
    // Chain: the press -> the launch read -> the refusal line; bounded so a hang fails here.
    expect(
      await screen.findByText(
        "This server's command cannot be approved for sharing, so it keeps running inside each session.",
        undefined,
        { timeout: 5000 },
      ),
    ).toBeTruthy()
    expect(setStub).not.toHaveBeenCalled()
  })

  it('says nothing was turned on when the launch read fails', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockRejectedValue(new Error('503'))
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Share with all my chats alpha-mcp' }))
    expect(await screen.findByText(/Nothing was turned on\./, undefined, { timeout: 5000 })).toBeTruthy()
    expect(setStub).not.toHaveBeenCalled()
  })

  it('reads the launch afresh on every press, so a press after a refusal pins the current one', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    const preview = vi
      .spyOn(api, 'mcpGatewayLaunchPreview')
      .mockResolvedValueOnce(launch() as never)
      .mockResolvedValue(launch({ commands: [['/opt/alpha', '--moved']], expected_launch: 'moved-identity' }) as never)
    const setStub = vi
      .spyOn(api, 'mcpGatewaySetStub')
      .mockRejectedValueOnce(new Error('launch_changed_since_display'))
      .mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)

    const { container } = mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Share with all my chats alpha-mcp' }))
    // Chain: the press -> launch read -> refused POST.
    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'alpha-identity'), { timeout: 5000 })
    const share = screen.getByRole('button', { name: 'Share with all my chats alpha-mcp' })
    await waitFor(() => expect((share as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(share)
    await waitFor(() => expect(setStub).toHaveBeenLastCalledWith('alpha-mcp', true, 'moved-identity'), { timeout: 5000 })
    expect(preview).toHaveBeenCalledTimes(2)
    expect(container.textContent).not.toContain('/opt/alpha')
  })

  it('lists the agents that declare a server when opened, and no command', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    const preview = vi.spyOn(api, 'mcpGatewayLaunchPreview')

    const { container } = mount()
    await screen.findByRole('button', { name: 'Stop sharing alpha-mcp' })
    const details = container.querySelector('details') as HTMLDetailsElement
    details.open = true
    fireEvent(details, new Event('toggle'))

    expect(await screen.findByText('Used by: kirocrew')).toBeTruthy()
    expect(preview).not.toHaveBeenCalled()
  })
})

describe('share all', () => {
  function mountTwo(enabled = true) {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled }) as never)
    const declared = {
      strength: 'declared',
      recommendStub: true,
      recommendShare: true,
      reasons: [{ code: 'preflight_passed', detail: '' }],
    }
    const servers = {
      servers: [
        { ...server(), recommendation: declared },
        { ...server({ name: 'beta-mcp' }), recommendation: declared },
      ],
    }
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue(servers as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockImplementation(
      async (name: string) =>
        launch({
          name,
          commands: [[`/opt/${name}`, '--serve']],
          expected_launch: `${name}-identity`,
        }) as never,
    )
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockImplementation(
      async (name: string) => ({ ok: true, name, stub: true, stubbed: [name] }) as never,
    )
    mount()
    return setStub
  }

  it('lists every server by name first, with no command, and shares nothing on the first press', async () => {
    const setStub = mountTwo()
    const all = await screen.findByRole('button', { name: 'Share all that can be shared' })
    await waitFor(() => expect((all as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(all)

    // Chain: servers re-read -> one launch read per candidate -> the list renders.
    const list = await screen.findByRole('region', { name: 'Servers to share' }, { timeout: 5000 })
    expect(list.textContent).toContain('alpha-mcp')
    expect(list.textContent).toContain('beta-mcp')
    expect(list.textContent).not.toContain('/opt/')
    expect(setStub).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Servers to share' })).toBeNull(), { timeout: 5000 })
    expect(setStub).not.toHaveBeenCalled()
  })

  it('reports a failed launch read as an error, not as nothing to share', async () => {
    const setStub = mountTwo()
    vi.mocked(api.mcpGatewayLaunchPreview).mockImplementation(async (name: string) => {
      if (name === 'beta-mcp') throw new Error('read failed')
      return launch({ name, commands: [[`/opt/${name}`, '--serve']], expected_launch: `${name}-identity` }) as never
    })
    const all = await screen.findByRole('button', { name: 'Share all that can be shared' })
    await waitFor(() => expect((all as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(all)

    // Chain: servers re-read -> one launch read per candidate -> error + list.
    expect(await screen.findByText(/Failed: beta-mcp\./, undefined, { timeout: 5000 })).toBeTruthy()
    const list = await screen.findByRole('region', { name: 'Servers to share' }, { timeout: 5000 })
    expect(list.textContent).not.toContain('beta-mcp')
    expect(setStub).not.toHaveBeenCalled()
  })

  it('shares exactly the launches it read for the servers it listed', async () => {
    const setStub = mountTwo()
    const all = await screen.findByRole('button', { name: 'Share all that can be shared' })
    await waitFor(() => expect((all as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(all)

    // Chain: servers re-read -> one launch read per candidate -> the confirm button.
    const confirm = await screen.findByRole('button', { name: /^Share the 2 listed$/ }, { timeout: 5000 })
    expect(confirm.tagName).toBe('BUTTON')
    fireEvent.click(confirm)

    // Chain: one stub POST per listed server, sent in turn.
    await waitFor(() => expect(setStub).toHaveBeenCalledTimes(2), { timeout: 5000 })
    expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'alpha-mcp-identity', true)
    expect(setStub).toHaveBeenCalledWith('beta-mcp', true, 'beta-mcp-identity', true)
  })

  it('reports shared and asks nothing when the writes say sharing is already on', async () => {
    const setStub = mountTwo(false)
    setStub.mockImplementation(async (name: string) => ({ ok: true, name, stub: true, stubbed: [name], sharing_on: true }) as never)
    vi.mocked(api.mcpGatewayStatus).mockResolvedValue(status({ enabled: false }) as never)
    const all = await screen.findByRole('button', { name: 'Share all that can be shared' })
    await waitFor(() => expect((all as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(all)
    // Another tab turns sharing on before this one confirms.
    vi.mocked(api.mcpGatewayStatus).mockResolvedValue(status({ enabled: true }) as never)
    fireEvent.click(await screen.findByRole('button', { name: /^Mark the 2 listed$/ }, { timeout: 5000 }))

    expect(await screen.findByText(/^Shared 2\./, undefined, { timeout: 5000 })).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('says it marks, not shares, the listed servers while sharing is off, then asks', async () => {
    const setStub = mountTwo(false)
    const enable = vi.spyOn(api, 'mcpGatewayEnable')
    const all = await screen.findByRole('button', { name: 'Share all that can be shared' })
    await waitFor(() => expect((all as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(all)
    fireEvent.click(await screen.findByRole('button', { name: /^Mark the 2 listed$/ }, { timeout: 5000 }))

    // Chain: one stub POST per listed server -> the result says marked -> the switch's confirm.
    await waitFor(() => expect(setStub).toHaveBeenCalledTimes(2), { timeout: 5000 })
    expect(await screen.findByText(/^Marked 2\./, undefined, { timeout: 5000 })).toBeTruthy()
    expect(screen.queryByText(/^Shared 2\./)).toBeNull()
    expect(await screen.findByRole('dialog', undefined, { timeout: 5000 })).toBeTruthy()
    expect(enable).not.toHaveBeenCalled()
  })

  it('puts the rows it re-read into the shared cache, so no view keeps a stale verdict', async () => {
    const declared = {
      strength: 'declared',
      recommendStub: true,
      recommendShare: true,
      reasons: [{ code: 'preflight_passed', detail: '' }],
    }
    const first = { servers: [{ ...server(), recommendation: declared }] }
    const fresh = {
      servers: [
        { ...server(), recommendation: declared },
        { ...server({ name: 'gamma-mcp' }), recommendation: declared },
      ],
    }
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValueOnce(first as never).mockResolvedValue(fresh as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockImplementation(
      async (name: string) => launch({ name, expected_launch: `${name}-identity` }) as never,
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    render(
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <McpManagement />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    const all = await screen.findByRole('button', { name: 'Share all that can be shared' })
    await waitFor(() => expect((all as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(all)

    // Chain: the fresh servers read -> the shared cache holds exactly that response.
    await screen.findByRole('region', { name: 'Servers to share' }, { timeout: 5000 })
    expect(qc.getQueryData(['mcpGatewayServers'])).toEqual(fresh)
  })

  it('names a candidate it leaves out inside the list', async () => {
    const setStub = mountTwo()
    vi.mocked(api.mcpGatewayLaunchPreview).mockImplementation(async (name: string) =>
      (name === 'beta-mcp'
        ? launch({ name, complete: false, expected_launch: undefined })
        : launch({ name, expected_launch: `${name}-identity` })) as never,
    )
    const all = await screen.findByRole('button', { name: 'Share all that can be shared' })
    await waitFor(() => expect((all as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(all)

    const list = await screen.findByRole('region', { name: 'Servers to share' }, { timeout: 5000 })
    expect(list.textContent).toContain('Skipped: beta-mcp.')
    expect(setStub).not.toHaveBeenCalled()
  })

  it('names the skipped servers, not "nothing left", when none of them can be approved', async () => {
    const setStub = mountTwo()
    vi.mocked(api.mcpGatewayLaunchPreview).mockImplementation(
      async (name: string) => launch({ name, complete: false, expected_launch: undefined }) as never,
    )
    const all = await screen.findByRole('button', { name: 'Share all that can be shared' })
    await waitFor(() => expect((all as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(all)

    // Chain: servers re-read -> every launch read is unapprovable -> the skipped names, no list.
    expect(await screen.findByText('Skipped: alpha-mcp, beta-mcp.', undefined, { timeout: 5000 })).toBeTruthy()
    expect(screen.queryByText('Nothing left to share.')).toBeNull()
    expect(screen.queryByRole('region', { name: 'Servers to share' })).toBeNull()
    expect(setStub).not.toHaveBeenCalled()
  })

})
