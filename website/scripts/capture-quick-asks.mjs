/**
 * Screenshots of the three QA-1 quick-ask variants, four scenes each, at
 * 1440x900 and 390x844, via capture/quick-asks.html, plus one side-by-side
 * overview per scene. Each shot asserts the live card and the variant's own
 * component rendered and that no CJK text is on the page.
 *
 * Usage (from website/): node scripts/capture-quick-asks.mjs <outDir>
 */
import { mkdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { chromium } from 'playwright'
import { createServer } from 'vite'

import { chromiumExecutable } from './lib/chromium-executable.mjs'

const out = resolve(process.argv[2] || '../temp-screenshots/quick-asks')
const SCENES = ['signin', 'robot', 'otp', 'purchase']
const FILE = { signin: 'signin', robot: 'robot', otp: 'otp', purchase: 'purchase' }
const SIZES = [['desktop', 1440, 900], ['phone', 390, 844]]
const MARK = { 1: 'quick-ask-inline', 2: { desktop: 'quick-ask-overlay', phone: 'quick-ask-bottom' }, 3: 'quick-ask-reply' }

const server = await createServer({ configFile: './vite.config.ts', server: { host: '127.0.0.1', port: 5231, strictPort: false }, logLevel: 'warn' })
await server.listen()
const base = server.resolvedUrls.local[0].replace(/\/$/, '')

const b = await chromium.launch({ executablePath: chromiumExecutable() })
for (const v of [1, 2, 3]) {
  mkdirSync(`${out}/v${v}`, { recursive: true })
  for (const scene of SCENES) {
    for (const [size, w, h] of SIZES) {
      const ctx = await b.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: 2, isMobile: size === 'phone', hasTouch: size === 'phone' })
      const p = await ctx.newPage()
      const problems = []
      p.on('pageerror', e => problems.push(String(e)))
      await p.goto(`${base}/capture/quick-asks.html?v=${v}&scene=${scene}&theme=dark`, { waitUntil: 'networkidle' })
      await p.locator('html[data-capture-ready="1"]').waitFor({ timeout: 60_000 })
      await p.locator('[data-testid="browser-live-card"][data-state="live"]').waitFor({ timeout: 20_000 })
      const mark = typeof MARK[v] === 'string' ? MARK[v] : MARK[v][size]
      await p.locator(`[data-testid="${mark}"]`).waitFor({ timeout: 10_000 })
      // Keep the ask and the card in view the way a person would see them.
      await p.locator(`[data-testid="browser-live-card"]`).evaluate((el, block) => el.scrollIntoView({ block }), v === 3 ? 'end' : 'start')
      await p.waitForTimeout(400)
      const cjk = await p.evaluate(() => /[\u3000-\u9fff\uac00-\ud7af]/.test(document.body.innerText))
      if (cjk) throw new Error(`v${v} ${scene} ${size}: CJK text on page`)
      if (problems.length) throw new Error(`v${v} ${scene} ${size}: ${problems.join('; ')}`)
      const file = `${out}/v${v}/${FILE[scene]}-${size}.png`
      await p.screenshot({ path: file })
      console.log('captured', file)
      await ctx.close()
    }
  }
}

// Overviews: the three variants side by side, desktop on top, phone below.
const img = f => `data:image/png;base64,${readFileSync(f).toString('base64')}`
const NAMES = { 1: 'V1 · Inline ask in the live card', 2: 'V2 · Focused ask sheet', 3: 'V3 · Structured reply in the composer' }
const TITLES = { signin: 'Sign in: username + password', robot: '"I\'m not a robot"', otp: '6-digit one-time code', purchase: 'Confirm purchase $35.00' }
for (const scene of SCENES) {
  const ctx = await b.newContext({ viewport: { width: 2400, height: 1200 }, deviceScaleFactor: 1 })
  const p = await ctx.newPage()
  const col = v => `<div class="c"><h2>${NAMES[v]}</h2><img class="d" src="${img(`${out}/v${v}/${FILE[scene]}-desktop.png`)}"><img class="m" src="${img(`${out}/v${v}/${FILE[scene]}-phone.png`)}"></div>`
  await p.setContent(`<html><body style="margin:0;background:#0d0d12;font-family:-apple-system,Helvetica,sans-serif;color:#eee">
    <style>.r{display:flex;gap:24px;padding:0 24px 24px}.c{flex:1;display:flex;flex-direction:column;align-items:center}h1{margin:0;padding:20px 24px 8px;font-size:26px}h2{font-size:18px;margin:8px 0 10px;color:#c8b8ff}.d{width:100%;border:1px solid #333;border-radius:8px}.m{width:42%;margin-top:14px;border:1px solid #333;border-radius:14px}</style>
    <h1>Quick asks · ${TITLES[scene]}</h1><div class="r">${col(1)}${col(2)}${col(3)}</div></body></html>`)
  await p.waitForLoadState('load')
  const file = `${out}/overview-${FILE[scene]}.png`
  await p.screenshot({ path: file, fullPage: true })
  console.log('captured', file)
  await ctx.close()
}
await b.close()
await server.close()
