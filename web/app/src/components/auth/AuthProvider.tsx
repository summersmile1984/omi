'use client';

import {
  createContext,
  useContext,
  useEffect,
  useState,
  ReactNode,
  useRef,
  useCallback,
} from 'react';
import type { WebAuthUser } from '@/lib/auth-types';
import {
  onAuthStateChange,
  signInWithGoogle,
  signInWithApple,
  signOutUser,
  getIdToken,
  setCompatCurrentUser,
} from '@/lib/firebase';
import {
  isBetterAuthEnabled,
  onBetterAuthStateChange,
  signOutBetterAuth,
} from '@/lib/better-auth';
import { MixpanelManager } from '@/lib/analytics/mixpanel';

interface AuthContextType {
  user: WebAuthUser | null;
  loading: boolean;
  signInWithGoogle: () => Promise<void>;
  signInWithApple: () => Promise<void>;
  signOut: () => Promise<void>;
  getToken: () => Promise<string | null>;
  // Login panel state
  isLoginPanelOpen: boolean;
  openLoginPanel: () => void;
  closeLoginPanel: () => void;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<WebAuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [isLoginPanelOpen, setIsLoginPanelOpen] = useState(false);
  const previousUserRef = useRef<WebAuthUser | null>(null);

  const openLoginPanel = useCallback(() => setIsLoginPanelOpen(true), []);
  const closeLoginPanel = useCallback(() => setIsLoginPanelOpen(false), []);

  useEffect(() => {
    // Initialize Mixpanel
    MixpanelManager.init();

    // Subscribe to auth state changes
    const onUser = (nextUser: WebAuthUser | null) => {
      setCompatCurrentUser(nextUser);
      setUser(nextUser);
      setLoading(false);

      // Identify user with Mixpanel when authenticated
      if (nextUser && !previousUserRef.current) {
        MixpanelManager.identify(nextUser.uid, {
          name: nextUser.displayName || undefined,
          email: nextUser.email || undefined,
        });
      }

      previousUserRef.current = nextUser;
    };
    const unsubscribe = isBetterAuthEnabled
      ? onBetterAuthStateChange(onUser)
      : onAuthStateChange(onUser);

    return () => unsubscribe();
  }, []);

  const handleSignInWithGoogle = async () => {
    try {
      if (isBetterAuthEnabled) throw new Error('Use email sign-in in Better Auth mode');
      await signInWithGoogle();
      MixpanelManager.track('Sign In Completed', { method: 'google' });
    } catch (error) {
      console.error('Failed to sign in with Google:', error);
      throw error;
    }
  };

  const handleSignInWithApple = async () => {
    try {
      if (isBetterAuthEnabled) throw new Error('Use email sign-in in Better Auth mode');
      await signInWithApple();
      MixpanelManager.track('Sign In Completed', { method: 'apple' });
    } catch (error) {
      console.error('Failed to sign in with Apple:', error);
      throw error;
    }
  };

  const handleSignOut = async () => {
    try {
      MixpanelManager.track('Sign Out');
      MixpanelManager.reset();
      if (isBetterAuthEnabled) await signOutBetterAuth();
      else await signOutUser();
    } catch (error) {
      console.error('Failed to sign out:', error);
      throw error;
    }
  };

  const handleGetToken = async () => {
    return getIdToken();
  };

  const value: AuthContextType = {
    user,
    loading,
    signInWithGoogle: handleSignInWithGoogle,
    signInWithApple: handleSignInWithApple,
    signOut: handleSignOut,
    getToken: handleGetToken,
    isLoginPanelOpen,
    openLoginPanel,
    closeLoginPanel,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
