// IPC surface for realtime-hub voice turns → the one kernel-owned transcript
// (INV-CHAT-1). The default (hub-native) voice path produces AND speaks a reply on
// the bar without ever reaching the kernel — so typed chat, the context tail, and
// mobile never see it. These two handlers close that gap the way macOS
// RealtimeHubController does:
//
//   * `voiceHub:recordTurn` records a COMPLETED hub turn (user + assistant) into the
//     SAME main_chat/chat/<chatId> conversation typed chat reads, origin
//     'realtime_voice'. That surface is the ONLY one whose turns appear in the typed
//     context tail (getMainChatTurnTail) — any other surfaceKind is a different
//     conversation. It records straight to the kernel store (no second transcript
//     store — INV-CHAT-1); the shared/mobile echo is the renderer's saveDesktopMessage.
//   * `voiceHub:getSeedContext` reads that same conversation back as the voice
//     session's continuity seed (read-only; never creates the conversation).
//
// OWNER AUTHORITY (INV-AGENT). The ownerId is host state (`controlPlaneOwnerId`),
// never taken off the renderer's args — same posture as agentControl.ts / mainChat.ts.
// Both handlers refuse while the owner is the shared DEFAULT_LOCAL_OWNER_ID (not
// signed in / the auth relay has not arrived) rather than key a conversation under
// the collision-prone default (the cold-start window mainChat.ts also fails closed on).

import { randomUUID } from 'node:crypto'
import { ipcMain, net, type WebContents } from 'electron'
import WebSocket from 'ws'
import {
  controlPlaneOwnerId,
  getAgentRuntimeKernel,
  hasKnownControlPlaneOwner
} from '../agentKernel/controlPlane'
import type { AgentRuntimeKernel } from '../agentKernel/kernel'
import type { SurfaceRef } from '../agentKernel/surfaceSession'
import type {
  VoiceHubRecordTurnArgs,
  VoiceHubRecordTurnResult,
  VoiceHubRelayContract,
  VoiceHubRelayEvent,
  VoiceHubRelayWireProtocol,
  VoiceHubSeedContext,
  VoiceHubSeedContextArgs
} from '../../shared/types'
import { getBackendSession, type BackendSession } from '../assistants/core/session'
import { resolveWindowsDeployment } from '../../shared/deploymentProfile'

/** Voice continuity seed window — a small Mac-parity window (macOS reads ~8 turns /
 *  3500 chars), deliberately distinct from the kernel's larger default so the seed
 *  the realtime instruction carries stays inside a low-latency budget. */
const VOICE_SEED_MAX_TURNS = 8
const VOICE_SEED_MAX_CHARACTERS = 3500
const RELAY_SUBPROTOCOL = 'omi.realtime.v1'
const RELAY_PENDING_TTL_MS = 30_000
const RELAY_MAX_PENDING_PER_RENDERER = 4

/** Bounded lifecycle for minted, connecting, and open relay leases. Kept
 * independent from Electron/WebSocket objects so TTL, owner cleanup, and the
 * per-renderer cap are behaviorally testable without constructing a BrowserWindow. */
export class VoiceHubRelayLeaseRegistry {
  private readonly leases = new Map<
    string,
    { ownerWebContentsId: number; timer: ReturnType<typeof setTimeout> | null; expire: () => void }
  >()

  constructor(
    private readonly ttlMs = RELAY_PENDING_TTL_MS,
    private readonly maxPerOwner = RELAY_MAX_PENDING_PER_RENDERER
  ) {}

  assertCapacity(ownerWebContentsId: number): void {
    const count = Array.from(this.leases.values()).filter(
      (lease) => lease.ownerWebContentsId === ownerWebContentsId
    ).length
    if (count >= this.maxPerOwner) throw new Error('too many pending realtime relay sessions')
  }

  reserve(connectionId: string, ownerWebContentsId: number, expire: () => void): void {
    this.assertCapacity(ownerWebContentsId)
    const timer = setTimeout(() => {
      const lease = this.leases.get(connectionId)
      if (!lease) return
      this.leases.delete(connectionId)
      lease.expire()
    }, this.ttlMs)
    timer.unref?.()
    this.leases.set(connectionId, { ownerWebContentsId, timer, expire })
  }

  markConnected(connectionId: string): void {
    const lease = this.leases.get(connectionId)
    if (!lease || !lease.timer) return
    clearTimeout(lease.timer)
    lease.timer = null
  }

  release(connectionId: string): void {
    const lease = this.leases.get(connectionId)
    if (!lease) return
    if (lease.timer) clearTimeout(lease.timer)
    this.leases.delete(connectionId)
  }

  cleanupOwner(ownerWebContentsId: number): void {
    for (const [connectionId, lease] of this.leases) {
      if (lease.ownerWebContentsId !== ownerWebContentsId) continue
      if (lease.timer) clearTimeout(lease.timer)
      this.leases.delete(connectionId)
      lease.expire()
    }
  }

  get size(): number {
    return this.leases.size
  }
}

type RelayFetch = (input: string, init?: RequestInit) => Promise<Response>
type PendingRelay = {
  ownerWebContentsId: number
  webContents: WebContents
  websocketUrl: string
  authorization: string
  socket: WebSocket | null
}

const pendingRelays = new Map<string, PendingRelay>()
const relayLeases = new VoiceHubRelayLeaseRegistry()
const relayOwnersWithCleanup = new Set<number>()

function expirePendingRelay(connectionId: string): void {
  const relay = pendingRelays.get(connectionId)
  pendingRelays.delete(connectionId)
  if (relay) relayEvent(connectionId, relay, { type: 'error', data: 'backend relay websocket handshake timed out' })
  relay?.socket?.close()
}

function ensureRelayOwnerCleanup(webContents: WebContents): void {
  if (relayOwnersWithCleanup.has(webContents.id)) return
  relayOwnersWithCleanup.add(webContents.id)
  webContents.once('destroyed', () => {
    relayOwnersWithCleanup.delete(webContents.id)
    relayLeases.cleanupOwner(webContents.id)
    for (const [connectionId, relay] of pendingRelays) {
      if (relay.ownerWebContentsId === webContents.id) expirePendingRelay(connectionId)
    }
  })
}

function relayEvent(connectionId: string, relay: PendingRelay, event: Omit<VoiceHubRelayEvent, 'connectionId'>): void {
  if (!relay.webContents.isDestroyed()) {
    relay.webContents.send('voiceHub:relayEvent', { connectionId, ...event } satisfies VoiceHubRelayEvent)
  }
}

export function resolveVoiceHubRelayUrl(apiBase: string, websocketPath: string): string {
  const backend = new URL(apiBase)
  if (backend.protocol !== 'https:') throw new Error('realtime relay requires an HTTPS backend origin')
  const websocketBase = new URL(backend.toString())
  websocketBase.protocol = 'wss:'
  const candidate = new URL(websocketPath, websocketBase)
  if (
    candidate.protocol !== 'wss:' ||
    candidate.hostname !== websocketBase.hostname ||
    candidate.port !== websocketBase.port ||
    candidate.username ||
    candidate.password
  ) {
    throw new Error('realtime relay returned a websocket outside the signed backend origin')
  }
  return candidate.toString()
}

export async function mintVoiceHubRelayContract(
  session: BackendSession,
  fetchImpl: RelayFetch = net.fetch
): Promise<{ websocketUrl: string; wireProtocol: VoiceHubRelayWireProtocol; authorization: string }> {
  const response = await fetchImpl(`${session.apiBase}/v2/realtime/session`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${session.token}` },
    body: JSON.stringify({ provider: 'relay' })
  })
  if (!response.ok) throw new Error(`realtime relay mint failed (status ${response.status})`)
  const payload = (await response.json()) as Record<string, unknown>
  if (payload.protocol !== RELAY_SUBPROTOCOL) throw new Error('realtime relay returned an unsupported protocol')
  if (payload.wire_protocol !== 'openai_realtime_v1') {
    throw new Error('realtime relay returned an unsupported wire protocol')
  }
  const websocketPath = payload.websocket_url
  if (typeof websocketPath !== 'string' || websocketPath.length === 0) {
    throw new Error('realtime relay returned no websocket URL')
  }
  return {
    websocketUrl: resolveVoiceHubRelayUrl(session.apiBase, websocketPath),
    wireProtocol: payload.wire_protocol,
    authorization: `Bearer ${session.token}`
  }
}

async function createVoiceHubRelay(webContents: WebContents): Promise<VoiceHubRelayContract> {
  const deployment = resolveWindowsDeployment()
  if (deployment.allowDirectModelProviders) {
    throw new Error('backend realtime relay is reserved for self-hosted deployments')
  }
  relayLeases.assertCapacity(webContents.id)
  const session = getBackendSession()
  if (!session) throw new Error('realtime relay requires an authenticated backend session')
  const minted = await mintVoiceHubRelayContract(session)
  const connectionId = randomUUID()
  relayLeases.reserve(connectionId, webContents.id, () => expirePendingRelay(connectionId))
  pendingRelays.set(connectionId, {
    ownerWebContentsId: webContents.id,
    webContents,
    websocketUrl: minted.websocketUrl,
    authorization: minted.authorization,
    socket: null
  })
  ensureRelayOwnerCleanup(webContents)
  return { connectionId, wireProtocol: minted.wireProtocol }
}

function ownedRelay(connectionId: string, webContents: WebContents): PendingRelay | null {
  const relay = pendingRelays.get(connectionId)
  return relay?.ownerWebContentsId === webContents.id ? relay : null
}

function connectVoiceHubRelay(connectionId: string, webContents: WebContents): void {
  const relay = ownedRelay(connectionId, webContents)
  if (!relay || relay.socket) return
  let socket: WebSocket
  try {
    socket = new WebSocket(relay.websocketUrl, RELAY_SUBPROTOCOL, {
      headers: { Authorization: relay.authorization }
    })
  } catch {
    relayLeases.release(connectionId)
    pendingRelays.delete(connectionId)
    relayEvent(connectionId, relay, { type: 'error', data: 'backend relay websocket construction failed' })
    return
  }
  relay.socket = socket
  socket.on('open', () => {
    relayLeases.markConnected(connectionId)
    relayEvent(connectionId, relay, { type: 'open' })
  })
  socket.on('message', (data) => relayEvent(connectionId, relay, { type: 'message', data: data.toString('utf8') }))
  socket.on('error', () => relayEvent(connectionId, relay, { type: 'error', data: 'backend relay websocket error' }))
  socket.on('close', (code, reason) => {
    relayLeases.release(connectionId)
    pendingRelays.delete(connectionId)
    relayEvent(connectionId, relay, { type: 'close', code, data: reason.toString('utf8') })
  })
}

function closeVoiceHubRelay(connectionId: string, webContents: WebContents): void {
  const relay = ownedRelay(connectionId, webContents)
  if (!relay) return
  relayLeases.release(connectionId)
  pendingRelays.delete(connectionId)
  relay.socket?.close()
}

/** What the handlers need from the host. Defaulted to the process-wide kernel and
 *  the main-side authoritative owner; injected in tests. */
export interface VoiceHubDeps {
  kernel: AgentRuntimeKernel
  ownerId: string
  ownerReady: boolean
}

function defaultDeps(): VoiceHubDeps {
  return {
    kernel: getAgentRuntimeKernel(),
    ownerId: controlPlaneOwnerId(),
    ownerReady: hasKnownControlPlaneOwner()
  }
}

function mainChatSurfaceRef(chatId?: string): SurfaceRef {
  return {
    surfaceKind: 'main_chat',
    externalRefKind: 'chat',
    externalRefId: chatId?.trim() || 'default'
  }
}

/**
 * Record a completed hub voice turn into the kernel transcript. Origin
 * 'realtime_voice'; the kernel appends one user + one assistant row and dedupes on
 * `idempotencyKey` (the per-press turnId) via a last-32 metadata scan, so a retried
 * or double-fired record is a no-op. Fire-and-forget from the renderer's view.
 */
export function recordVoiceHubTurn(
  args: VoiceHubRecordTurnArgs,
  deps: VoiceHubDeps = defaultDeps()
): VoiceHubRecordTurnResult {
  if (!deps.ownerReady) return { recorded: false, duplicate: false, reason: 'owner_not_ready' }
  const userText = (args.userText ?? '').trim()
  const assistantText = (args.assistantText ?? '').trim()
  if (!userText && !assistantText) return { recorded: false, duplicate: false, reason: 'empty' }

  const result = deps.kernel.recordSurfaceTurn({
    ownerId: deps.ownerId,
    surfaceRef: mainChatSurfaceRef(args.chatId),
    userText,
    assistantText,
    origin: 'realtime_voice',
    interrupted: args.interrupted === true,
    idempotencyKey: args.idempotencyKey
  })
  return {
    recorded: result.recorded,
    duplicate: result.duplicate,
    conversationId: result.conversationId
  }
}

/**
 * Read the voice continuity seed for the main_chat conversation. Read-only: an
 * absent conversation returns an empty seed, so a renderer seed refresh never
 * writes to the store.
 */
export function readVoiceHubSeedContext(
  args: VoiceHubSeedContextArgs,
  deps: VoiceHubDeps = defaultDeps()
): VoiceHubSeedContext {
  if (!deps.ownerReady) return { context: '', idempotencyKeys: [] }
  const snapshot = deps.kernel.getVoiceSeedContextForMainChat({
    ownerId: deps.ownerId,
    chatId: args.chatId,
    maxTurns: VOICE_SEED_MAX_TURNS,
    maxCharacters: VOICE_SEED_MAX_CHARACTERS
  })
  return { context: snapshot.context, idempotencyKeys: snapshot.idempotencyKeys }
}

export function registerVoiceHubHandlers(): void {
  ipcMain.handle(
    'voiceHub:recordTurn',
    (_e, args: VoiceHubRecordTurnArgs): VoiceHubRecordTurnResult => recordVoiceHubTurn(args)
  )
  ipcMain.handle(
    'voiceHub:getSeedContext',
    (_e, args: VoiceHubSeedContextArgs = {}): VoiceHubSeedContext => readVoiceHubSeedContext(args)
  )
  ipcMain.handle('voiceHub:relayCreate', (event) => createVoiceHubRelay(event.sender))
  ipcMain.on('voiceHub:relayConnect', (event, connectionId: string) =>
    connectVoiceHubRelay(connectionId, event.sender)
  )
  ipcMain.on('voiceHub:relaySend', (event, connectionId: string, data: string) => {
    const relay = ownedRelay(connectionId, event.sender)
    if (relay?.socket?.readyState === WebSocket.OPEN) relay.socket.send(data)
  })
  ipcMain.on('voiceHub:relayClose', (event, connectionId: string) =>
    closeVoiceHubRelay(connectionId, event.sender)
  )
}
