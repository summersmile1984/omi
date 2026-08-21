// IPC surface for delegated coding-agent tasks. Follows the house pattern:
// invoke-style handlers plus a broadcast channel for streaming task events
// (both the main window and the overlay may render the same task's progress).

import { ipcMain, BrowserWindow, shell } from 'electron'
import {
  ADAPTER_PROFILES,
  adapterActivationError,
  adapterIsActivated,
  type AdapterCommandOverrides
} from '../codingAgent/adapterRegistry'
import { PRODUCTION_ADAPTER_IDS } from '../codingAgent/interface'
import { cancelTask, runCodingAgentTask, testAgentConnection } from '../codingAgent/taskRunner'
import {
  claudeAuthStatus,
  removeClaudeCredentials,
  startClaudeOAuthFlow,
  validateClaudeOAuthUrl,
  type ClaudeOAuthFlowHandle
} from '../codingAgent/claudeOAuth'
import { messageFrom } from '../codingAgent/failures'
import { detectAgents } from '../codingAgent/agentDetect'
import { codexApiKeyStatus, saveCodexApiKey } from '../codingAgent/codexAuth'
import type { ProductionAdapterId } from '../codingAgent/interface'
import type {
  AgentDetectionMap,
  CodexKeyResult,
  CodexKeyStatus,
  CodingAgentAuthStatus,
  CodingAgentEvent,
  CodingAgentInfo,
  CodingAgentResult,
  CodingAgentRunArgs,
  CodingAgentStartAuthResult
} from '../../shared/types'
import { resolveWindowsDeployment } from '../../shared/deploymentProfile'

function allowExternalCodingAgents(): boolean {
  return resolveWindowsDeployment().allowDirectModelProviders
}

function broadcast(event: CodingAgentEvent): void {
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) {
      win.webContents.send('codingAgent:event', event)
    }
  }
}

export function registerCodingAgentHandlers(): void {
  ipcMain.handle(
    'codingAgent:list',
    (_e, commandOverrides?: AdapterCommandOverrides): CodingAgentInfo[] => {
      if (!allowExternalCodingAgents()) return []
      const overrides = commandOverrides ?? {}
      return PRODUCTION_ADAPTER_IDS.map((id) => {
        const connected = adapterIsActivated(id, overrides)
        return {
          id,
          displayName: ADAPTER_PROFILES[id].displayName,
          connected,
          installHint: connected ? undefined : adapterActivationError(id)
        }
      })
    }
  )

  ipcMain.handle(
    'codingAgent:run',
    (_e, args: CodingAgentRunArgs): Promise<CodingAgentResult> => {
      if (!allowExternalCodingAgents()) {
        return Promise.resolve({
          taskId: args.taskId,
          ok: false,
          adapterId: null,
          text: '',
          error: 'External coding-agent processes are disabled by this deployment profile.'
        })
      }
      return runCodingAgentTask(args, broadcast, (message) => console.log(`[codingAgent] ${message}`))
    }
  )

  ipcMain.handle('codingAgent:cancel', (_e, taskId: string): boolean =>
    allowExternalCodingAgents() ? cancelTask(taskId) : false
  )

  ipcMain.handle(
    'codingAgent:test',
    (_e, agentId: ProductionAdapterId, commandOverrides?: AdapterCommandOverrides) =>
      allowExternalCodingAgents()
        ? testAgentConnection(agentId, commandOverrides ?? {}, (message) =>
            console.log(`[codingAgent] ${message}`)
          )
        : Promise.resolve({ ok: false, error: 'External coding agents are disabled.' })
  )

  ipcMain.handle('codingAgent:authStatus', (): CodingAgentAuthStatus =>
    allowExternalCodingAgents() ? claudeAuthStatus() : { connected: false, expiresAt: null }
  )

  ipcMain.handle(
    'codingAgent:startAuth',
    (): Promise<CodingAgentStartAuthResult> =>
      allowExternalCodingAgents()
        ? startClaudeAuth()
        : Promise.resolve({
            ok: false,
            error: 'External coding-agent authentication is disabled.',
            status: { connected: false, expiresAt: null }
          })
  )

  ipcMain.handle('codingAgent:signOut', (): CodingAgentAuthStatus => {
    if (!allowExternalCodingAgents()) return { connected: false, expiresAt: null }
    removeClaudeCredentials()
    return claudeAuthStatus()
  })

  // PATH auto-detection for the external agent CLIs (Codex / Hermes / OpenClaw).
  ipcMain.handle('codingAgent:detect', (): Promise<AgentDetectionMap> =>
    allowExternalCodingAgents()
      ? detectAgents()
      : Promise.resolve({
          openclaw: { installed: false },
          hermes: { installed: false },
          codex: { installed: false }
        })
  )

  // Codex OpenAI API-key lane (encrypted store, boolean-only status to renderer).
  ipcMain.handle('codingAgent:codexKeyStatus', (): CodexKeyStatus => codexApiKeyStatus())
  ipcMain.handle(
    'codingAgent:setCodexKey',
    (_e, key: string): Promise<CodexKeyResult> =>
      saveCodexApiKey(typeof key === 'string' ? key : '')
  )
}

// One in-flight Claude sign-in at a time. A duplicate request (e.g. the user
// double-clicks "Sign in") joins the running flow instead of opening a second
// browser tab or spinning up a second callback server — mirrors macOS's
// idempotent startAuthFlow / one-launch latch.
let activeAuth: Promise<CodingAgentStartAuthResult> | null = null

async function startClaudeAuth(): Promise<CodingAgentStartAuthResult> {
  if (activeAuth) return activeAuth
  activeAuth = runClaudeAuthOnce().finally(() => {
    activeAuth = null
  })
  return activeAuth
}

async function runClaudeAuthOnce(): Promise<CodingAgentStartAuthResult> {
  const log = (message: string): void => console.log(`[codingAgent] ${message}`)
  let flow: ClaudeOAuthFlowHandle | null = null
  try {
    flow = await startClaudeOAuthFlow(log)
    // Validate before opening: never hand the browser a URL that isn't the
    // exact claude.ai PKCE loopback authorize request we built.
    const validated = validateClaudeOAuthUrl(flow.authUrl)
    if (!validated) {
      flow.cancel()
      // Fail-closed (macOS parity): don't hand the browser a URL that isn't the
      // exact claude.ai PKCE loopback request; surface the same generic copy.
      return {
        ok: false,
        error: 'Unable to start Claude sign-in. Try again.',
        status: claudeAuthStatus()
      }
    }
    // Don't spawn a real browser under E2E (keeps the harness hermetic and lets
    // it screenshot the upsell sheet without a claude.ai tab opening).
    if (!process.env.OMI_E2E) void shell.openExternal(validated.toString())
    await flow.complete
    return { ok: true, status: claudeAuthStatus() }
  } catch (error) {
    flow?.cancel()
    return { ok: false, error: messageFrom(error), status: claudeAuthStatus() }
  }
}
