import { readFileSync, writeFileSync, renameSync, mkdirSync } from 'node:fs'
import { dirname } from 'node:path'
import { IdentityError } from './contract'
import { parseUser, type Credential } from './client'

export interface StoredIdentity {
  credential: Credential | null
  lastUserId: string | null
  clearEpoch: number
  cleanupPending: boolean
}
export interface CredentialStore {
  read(): StoredIdentity
  write(value: StoredIdentity): void
}
export interface Encryption {
  isEncryptionAvailable(): boolean
  getSelectedStorageBackend?(): string
  encryptString(value: string): Buffer
  decryptString(value: Buffer): string
}

// Refuse Electron's Linux basic_text backend: availability alone also reports
// true for this fixed-password fallback. Corruption is not "signed out".
export class SecureCredentialStore implements CredentialStore {
  constructor(
    private file: string,
    private identity: string,
    private encryption: Encryption,
    private platform: NodeJS.Platform
  ) {}
  private check(): void {
    if (
      !this.encryption.isEncryptionAvailable() ||
      (this.platform === 'linux' &&
        !['gnome_libsecret', 'kwallet', 'kwallet5', 'kwallet6'].includes(
          this.encryption.getSelectedStorageBackend?.() ?? 'unknown'
        ))
    ) {
      throw new IdentityError('storage_unavailable')
    }
  }
  read(): StoredIdentity {
    this.check()
    let bytes: Buffer
    try {
      bytes = readFileSync(this.file)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT')
        return { credential: null, lastUserId: null, clearEpoch: 0, cleanupPending: false }
      throw new IdentityError('storage_unavailable')
    }
    try {
      const value = JSON.parse(this.encryption.decryptString(bytes))
      if (
        !Number.isSafeInteger(value.clearEpoch) ||
        value.clearEpoch < 0 ||
        typeof value.cleanupPending !== 'boolean' ||
        value.version !== 1 ||
        value.identity !== this.identity ||
        (value.lastUserId !== null && typeof value.lastUserId !== 'string')
      )
        throw new Error()
      const c = value.credential
      if (c !== null && (typeof c?.session !== 'string' || !c.session)) throw new Error()
      const credential = c ? { session: c.session, user: parseUser(c.user) } : null
      if (credential && (value.cleanupPending || credential.user.id !== value.lastUserId))
        throw new Error()
      return {
        lastUserId: value.lastUserId,
        clearEpoch: value.clearEpoch,
        cleanupPending: value.cleanupPending,
        credential
      }
    } catch {
      throw new IdentityError('storage_corrupt')
    }
  }
  write(value: StoredIdentity): void {
    this.check()
    try {
      const bytes = this.encryption.encryptString(
        JSON.stringify({ version: 1, identity: this.identity, ...value })
      )
      mkdirSync(dirname(this.file), { recursive: true, mode: 0o700 })
      const tmp = this.file + '.tmp'
      writeFileSync(tmp, bytes, { mode: 0o600 })
      renameSync(tmp, this.file)
    } catch {
      throw new IdentityError('storage_unavailable')
    }
  }
}
