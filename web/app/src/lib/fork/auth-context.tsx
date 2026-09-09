'use client';

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from 'react';
import { authSession, type AppUser, type AuthSession } from './auth-session';

interface AuthContextType {
  user: AppUser | null;
  loading: boolean;
  authError: string | null;
  signInWithEmail: (email: string, password: string) => Promise<void>;
  signUpWithEmail: (name: string, email: string, password: string) => Promise<void>;
  signInWithGoogle: () => Promise<void>;
  signInWithApple: () => Promise<void>;
  signOut: () => Promise<void>;
  getToken: () => Promise<string | null>;
  retrySession: () => Promise<void>;
  isLoginPanelOpen: boolean;
  openLoginPanel: () => void;
  closeLoginPanel: () => void;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);
const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : 'Sign-in is unavailable. Please try again.';
const unconfiguredSocialProvider = async () => {
  throw new Error('This sign-in provider is not configured for this deployment.');
};

export function AuthProvider({
  children,
  session,
}: {
  children: ReactNode;
  session?: AuthSession;
}) {
  const [user, setUser] = useState<AppUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [authError, setAuthError] = useState<string | null>(null);
  const [isLoginPanelOpen, setLoginPanelOpen] = useState(false);
  const controller = useCallback(() => session ?? authSession(), [session]);
  const retrySession = useCallback(async () => {
    setLoading(true);
    setAuthError(null);
    try {
      await controller().restore();
    } catch (error) {
      setAuthError(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }, [controller]);

  useEffect(() => {
    let unsubscribe: (() => void) | undefined;
    try {
      unsubscribe = controller().subscribe(setUser);
    } catch (error) {
      setAuthError(errorMessage(error));
      setLoading(false);
      return;
    }
    void retrySession();
    return unsubscribe;
  }, [retrySession, controller]);

  const value: AuthContextType = {
    user,
    loading,
    authError,
    isLoginPanelOpen,
    retrySession,
    signInWithEmail: async (email, password) => {
      await controller().signIn(email, password);
      setAuthError(null);
    },
    signUpWithEmail: async (name, email, password) => {
      await controller().signUp(name, email, password);
      setAuthError(null);
    },
    signInWithGoogle: unconfiguredSocialProvider,
    signInWithApple: unconfiguredSocialProvider,
    signOut: () => controller().signOut(),
    getToken: () => controller().getToken(),
    openLoginPanel: () => setLoginPanelOpen(true),
    closeLoginPanel: () => setLoginPanelOpen(false),
  };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextType {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used within an AuthProvider.');
  return context;
}
