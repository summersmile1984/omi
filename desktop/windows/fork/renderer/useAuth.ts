import { useEffect, useState } from 'react'
import { auth, onAuthStateChanged, type User } from './identity'

export function useAuth(): { user: User | null; loading: boolean } {
  const [state, setState] = useState({ user: auth.currentUser, loading: true })
  useEffect(() => onAuthStateChanged(auth, (user) => setState({ user, loading: false })), [])
  return state
}
