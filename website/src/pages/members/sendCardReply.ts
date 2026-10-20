import { api } from '../../api/client'
import { sendTurn, type SendWire } from '../../chat-core/transport/sendTurn'
import { mintSendId } from '../../utils/sendDelivery'

/** The fetch seam for a Dashboard card's reply: the usual `POST /api/chat?ws=1`, plus
 *  `origin: 'dashboard_card'`, which the gateway checks again and which tags the turn. */
const cardSendWire: SendWire = (payload, signal) =>
  api.sendChat(payload.message, payload.slot, undefined, signal, payload.meta, undefined, undefined, 'dashboard_card')

/**
 * Send a reply a Dashboard-tab button offered into the crewmate's own thread.
 *
 * Resolves `'sent'` when the gateway took it (or may have: a late or unreadable
 * answer is not a refusal, and filling the composer then would invite a second
 * send). `'refused'` is the gateway's own routine "not now" (a `card_*` code: the
 * Profile switch is off, the thread is busy or waiting on an approval or a question,
 * or a send went through moments ago). `'failed'` is anything else: a server error,
 * an expired sign-in, a request that never left. For both, the caller fills the
 * composer and the person presses send, which is what a button did before one-click
 * sends existed; only `'failed'` is also shown to the person as an error.
 */
export type CardSendOutcome = 'sent' | 'refused' | 'failed'

export async function sendCardReply(slot: string, text: string): Promise<CardSendOutcome> {
  const receipt = await sendTurn({
    message: text,
    slot,
    // Echoed back by the gateway, so the sent row appears in this tab.
    meta: { sendId: mintSendId() },
    wire: cardSendWire,
  })
  if (receipt.status === 'transport-error') return 'failed'
  if (receipt.status === 'refused') {
    const code = (receipt.body as { code?: unknown }).code
    return typeof code === 'string' && code.startsWith('card_') ? 'refused' : 'failed'
  }
  return 'sent'
}
