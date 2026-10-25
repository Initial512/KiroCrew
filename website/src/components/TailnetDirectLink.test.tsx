import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { TailnetDirectLink } from './TailnetDirectLink'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn(async () => false) }))

describe('TailnetDirectLink', () => {
  it('a refused copy shows the shared error notice', async () => {
    render(<TailnetDirectLink direct={{ state: 'active', urls: ['https://crew.example.ts.net/'] } as never} />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    const notice = await screen.findByTestId('direct-link-copy-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(screen.getByRole('button', { name: /Ask the agent/ })).toBeInTheDocument()
  })
})
