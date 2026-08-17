/**
 * Evidence for the Continue-refusal notice.
 *
 * THE BUG: pressing Continue on a failed turn could be refused by the server
 * (readiness gate, or a slot-lock re-check like `slot_subagents_running`) and the
 * refusal went to `console.warn`, so the button flicked to disabled and straight
 * back with nothing on screen.
 *
 * The scene composes the REAL surface the press happens on, out of `src/`: the
 * error card that hosts one Continue button, the notice wrapper ChatPage renders
 * above the composer, and the composer itself carrying the other Continue. The
 * only difference between the two scenes is whether a refusal string is present,
 * which is exactly the state `continueError` holds — nothing here re-implements
 * the components or their classes.
 *
 *   ?scene=before|after   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import ChatInput from '../src/components/ChatInput'
import ErrorNotice from '../src/components/ErrorNotice'
import { initI18n } from '../src/i18n'
import { ErrorCard } from '../src/pages/chat/ErrorCard'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') === 'before' ? 'before' : 'after'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/')) {
    // The composer's slash-command menu maps over its response, so an object
    // stub crashes it and the harness screenshots an unmounted tree. Arrays for
    // list endpoints, `{}` for the rest.
    const body = /commands|models|agents|skills|slots|projects/.test(url) ? '[]' : '{}'
    return Promise.resolve(new Response(body, { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

/** The reason a real refusal carries — the server's own prose, verbatim. */
const REFUSAL = 'Kiro CLI setup or sign-in is required before starting a session.'

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <div data-capture-root className="bg-bg text-text flex flex-col gap-2 py-5 w-[900px]">
          <div className="px-5 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
            <ErrorCard
              content="⟳ The turn ended without a reply — connection lost."
              onContinue={() => {}}
            />
          </div>
          {scene === 'after' && (
            <div
              className="px-5 mb-1.5 mx-auto w-full"
              style={{ maxWidth: 'var(--mc-content-width, 900px)' }}
              data-testid="continue-error"
            >
              <ErrorNotice message={REFUSAL} onDismiss={() => {}} />
            </div>
          )}
          {/* The composer's real container in ChatPage — the notice above mirrors
              it, so the frame shows the true alignment rather than a harness one. */}
          <div className="px-5 pb-2 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
            <ChatInput
              value=""
              onChange={() => {}}
              onSend={() => {}}
              connected
              continuable
              continueIsRecovery
              onContinue={() => {}}
            />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
