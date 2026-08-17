/**
 * Screenshots of the Continue-refusal notice.
 *
 * Drives the isolated capture entry (website/capture/continue-refusal.html), which
 * mounts the REAL ErrorCard, ErrorNotice and ChatInput against the real stylesheet
 * and theme tokens. Not the full SPA: reaching this state in the shell needs a
 * seeded session, a live websocket and a server that refuses, and a half-stubbed
 * shell screenshots its error boundary instead — worse evidence than none.
 *
 * Each scene asserts its RENDERED TEXT before writing the file, so a run can never
 * emit a frame that contradicts the diff (an empty notice, or a composer that lost
 * its Resume button).
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6811 --strictPort   # in another shell
 *   node scripts/capture-continue-refusal.mjs http://127.0.0.1:6811 ../temp-screenshots/continue-refusal
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6811'
const OUT = process.argv[3] || '../temp-screenshots/continue-refusal'
mkdirSync(OUT, { recursive: true })

const REFUSAL = 'Kiro CLI setup or sign-in is required before starting a session.'

const SCENES = [
  { name: 'before-dark', scene: 'before', theme: 'dark', notice: false },
  { name: 'after-dark', scene: 'after', theme: 'dark', notice: true },
  { name: 'after-light', scene: 'after', theme: 'light', notice: true },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 960, height: 420 }, deviceScaleFactor: 2 })

let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/continue-refusal.html?scene=${s.scene}&theme=${s.theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.waitForSelector('[data-testid="error-card-continue"]')
  // Both Continue buttons must be present in every scene: the notice is an
  // addition to the surface, never a replacement for the affordance.
  const composer = await page.locator('[data-testid="composer-continue"]').count()
  const notice = await page.locator('[data-testid="continue-error"]').count()
  const text = notice ? (await page.locator('[data-testid="continue-error"]').innerText()).trim() : ''
  const ok = composer === 1 && notice === (s.notice ? 1 : 0) && (!s.notice || text.includes(REFUSAL))
  console.log(`${s.name}: composer-continue=${composer} notice=${notice} text=${JSON.stringify(text)} ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) { failed = true; continue }
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${s.name}.png` })
}

await browser.close()
if (failed) {
  console.error('one or more scenes did not render the expected state — no misleading frame written')
  process.exit(1)
}
console.log(`wrote ${SCENES.length} screenshots to ${OUT}`)
