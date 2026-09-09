import { contextBridge, ipcRenderer } from 'electron'
import type { IdentityBridge, IdentitySnapshot } from './contract'

const bridge: IdentityBridge = {
  snapshot: () => ipcRenderer.invoke('identity:snapshot'),
  authenticate: (input) => ipcRenderer.invoke('identity:authenticate', input),
  token: (owner, force) => ipcRenderer.invoke('identity:token', owner, force),
  updateName: (owner, name) => ipcRenderer.invoke('identity:name', owner, name),
  signOut: (owner) => ipcRenderer.invoke('identity:signOut', owner),
  invalidate: (owner) => ipcRenderer.invoke('identity:invalidate', owner),
  onChanged(callback) {
    const listener = (_event: Electron.IpcRendererEvent, snapshot: IdentitySnapshot): void =>
      callback(snapshot)
    ipcRenderer.on('identity:changed', listener)
    return () => ipcRenderer.removeListener('identity:changed', listener)
  }
}
contextBridge.exposeInMainWorld('forkIdentity', bridge)
