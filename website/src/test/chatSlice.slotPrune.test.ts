import { describe, it, expect } from 'vitest'
import chatReducer, { deleteSlot } from '../store/chatSlice'
import notifReducer, { addNotification, fetchNotifications, NOTIFICATIONS_RING_CAP } from '../store/notificationsSlice'
import { sseSlots } from '../store/dashboardSlice'
import type { ChatMessage, ChatSlot, Notification } from '../types'
import './mockApiClient'

const slot = (key: string): ChatSlot => ({ key, messages: 0, running: false })
const msg = (content: string): ChatMessage => ({ role: 'assistant', content, cls: '' })

/** Seed a chat state carrying per-slot caches for the given keys. */
function seeded(keys: string[], activeSlot: string | null = null) {
  const initial = chatReducer(undefined, { type: '@@INIT' })
  const state = {
    ...initial,
    activeSlot,
    slotMessages: Object.fromEntries(keys.map(k => [k, [msg(`hi from ${k}`)]])),
    slotActivity: Object.fromEntries(keys.map(k => [k, { toolLog: [], subagents: {} }])),
    slotRun: Object.fromEntries(keys.map(k => [k, { state: 'idle' as const }])),
    slotHydrated: Object.fromEntries(keys.map(k => [k, true])),
    slotSide: Object.fromEntries(keys.map(k => [k, { messages: [], openedAtTurnCount: 0, createdAt: '2026-01-01' }])),
    slotSideClosed: Object.fromEntries(keys.map(k => [k, false])),
    slotHistory: [...keys],
  }
  return state
}

describe('chatSlice sseSlots reconciliation', () => {
  it('prunes per-slot caches for slots absent from the authoritative list', () => {
    const state = seeded(['chat-1', 'chat-2', 'chat-3'])
    const next = chatReducer(state, sseSlots([slot('chat-1'), slot('chat-3')]))
    expect(Object.keys(next.slotMessages).sort()).toEqual(['chat-1', 'chat-3'])
    expect(next.slotActivity['chat-2']).toBeUndefined()
    expect(next.slotRun['chat-2']).toBeUndefined()
    expect(next.slotHydrated['chat-2']).toBeUndefined()
    expect(next.slotSide['chat-2']).toBeUndefined()
    expect(next.slotSideClosed['chat-2']).toBeUndefined()
    expect(next.slotHistory).toEqual(['chat-1', 'chat-3'])
  })

  it('never prunes the active slot even when absent from the list', () => {
    const state = seeded(['chat-1', 'chat-2'], 'chat-2')
    const next = chatReducer(state, sseSlots([slot('chat-1')]))
    expect(next.slotMessages['chat-2']).toBeDefined()
    expect(next.slotMessages['chat-2'][0].content).toBe('hi from chat-2')
  })

  it('treats an empty slots payload as a no-op (SSE reconnect guard)', () => {
    const state = seeded(['chat-1', 'chat-2'])
    const next = chatReducer(state, sseSlots([]))
    expect(Object.keys(next.slotMessages)).toEqual(['chat-1', 'chat-2'])
    expect(next.slotHistory).toEqual(['chat-1', 'chat-2'])
  })

  it('prunes keys present only in sibling maps (no slotMessages entry)', () => {
    const state = seeded(['chat-1'])
    state.slotRun['ghost'] = { state: 'idle' }
    state.slotActivity['ghost'] = { toolLog: [], subagents: {} }
    const next = chatReducer(state, sseSlots([slot('chat-1')]))
    expect(next.slotRun['ghost']).toBeUndefined()
    expect(next.slotActivity['ghost']).toBeUndefined()
  })

  it('prunes the small per-slot maps too (statusDetail, contextPct, contextTokens, stopPressedAt)', () => {
    const state = seeded(['chat-1', 'chat-2'])
    state.slotStatusDetail = { 'chat-2': { kind: 'compacting', text: 'Compacting…', ts: 1 } }
    state.slotContextPct = { 'chat-2': 42 }
    state.slotContextTokens = { 'chat-2': { used: 1234, window: 200000 } }
    state.stopPressedAt = { 'chat-2': 999 }
    const next = chatReducer(state, sseSlots([slot('chat-1')]))
    expect(next.slotStatusDetail['chat-2']).toBeUndefined()
    expect(next.slotContextPct['chat-2']).toBeUndefined()
    expect(next.slotContextTokens['chat-2']).toBeUndefined()
    expect(next.stopPressedAt['chat-2']).toBeUndefined()
  })
})

describe('slot teardown parity', () => {
  /** Seed every map the store keys per slot, so a teardown path that forgets
   *  one of them leaves a visible entry behind. */
  function richlySeeded(keys: string[]) {
    const base = seeded(keys)
    return {
      ...base,
      slotStatusDetail: Object.fromEntries(keys.map(k => [k, { kind: 'compacting' as const, text: 'Compacting…', ts: 1 }])),
      slotContextPct: Object.fromEntries(keys.map(k => [k, 42])),
      slotContextTokens: Object.fromEntries(keys.map(k => [k, { used: 1234, window: 200000 }])),
      stopPressedAt: Object.fromEntries(keys.map(k => [k, 999])),
      followups: Object.fromEntries(keys.map(k => [k, { items: [], ts: 1 }])),
      folderSuggestions: Object.fromEntries(keys.map(k => [k, { folderId: 'f', folderName: 'F', breadcrumb: 'F', ts: 1 }])),
      subagentQueued: Object.fromEntries(keys.map(k => [k, 2])),
      goalLoops: Object.fromEntries(keys.map(k => [k, { cycle_count: 1, max_cycles: 5 }])),
    }
  }

  const perSlotMaps = [
    'slotMessages', 'slotActivity', 'slotRun', 'slotHydrated', 'slotSide',
    'slotSideClosed', 'slotStatusDetail', 'slotContextPct', 'slotContextTokens',
    'stopPressedAt', 'followups', 'folderSuggestions', 'subagentQueued', 'goalLoops',
  ] as const

  const keysOf = (state: unknown, map: string) =>
    Object.keys((state as Record<string, Record<string, unknown>>)[map]).sort()

  it('deleting a slot leaves no entry in any per-slot map', () => {
    const state = richlySeeded(['chat-1', 'chat-2'])
    const next = chatReducer(state, { type: deleteSlot.fulfilled.type, payload: 'chat-2' })
    for (const map of perSlotMaps) {
      expect(keysOf(next, map)).toEqual(['chat-1'])
    }
    expect(next.slotHistory).toEqual(['chat-1'])
  })

  /** The two teardown paths read one shared list of per-slot maps; this fails
   *  if a map is ever registered with only one of them. */
  it('the reconcile evicts exactly what deleting evicts', () => {
    const seed = richlySeeded(['chat-1', 'chat-2'])
    const deleted = chatReducer(seed, { type: deleteSlot.fulfilled.type, payload: 'chat-2' })
    const reconciled = chatReducer(seed, sseSlots([slot('chat-1')]))
    for (const map of perSlotMaps) {
      expect(keysOf(reconciled, map)).toEqual(keysOf(deleted, map))
    }
    expect(reconciled.slotHistory).toEqual(deleted.slotHistory)
  })
})

describe('notificationsSlice ring cap', () => {
  const notif = (ts: number): Notification => ({ kind: 'cron', title: `t${ts}`, body: 'b', ts: String(ts) })

  it('caps items at NOTIFICATIONS_RING_CAP, dropping oldest first', () => {
    let state = notifReducer(undefined, { type: '@@INIT' })
    for (let i = 0; i < NOTIFICATIONS_RING_CAP + 25; i++) {
      state = notifReducer(state, addNotification(notif(i)))
    }
    expect(state.items).toHaveLength(NOTIFICATIONS_RING_CAP)
    expect(state.items[0].ts).toBe('25')
    expect(state.items[state.items.length - 1].ts).toBe(String(NOTIFICATIONS_RING_CAP + 24))
  })

  it('still dedupes by ts under the cap', () => {
    let state = notifReducer(undefined, { type: '@@INIT' })
    state = notifReducer(state, addNotification(notif(1)))
    state = notifReducer(state, addNotification(notif(1)))
    expect(state.items).toHaveLength(1)
  })

  it('caps the fetch path too, keeping the newest entries', () => {
    const items = Array.from({ length: NOTIFICATIONS_RING_CAP + 50 }, (_, i) => notif(i))
    // seq 0 matches the initial clear generation, so the payload is applied.
    const payload = { items, seq: 0 }
    const state = notifReducer(undefined, { type: fetchNotifications.fulfilled.type, payload })
    expect(state.items).toHaveLength(NOTIFICATIONS_RING_CAP)
    expect(state.items[0].ts).toBe('50')
  })
})
