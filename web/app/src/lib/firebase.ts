import { initializeApp, getApps } from 'firebase/app';
import {
  getAuth,
  type Auth,
  GoogleAuthProvider,
  OAuthProvider,
  signInWithPopup,
  signOut,
  onAuthStateChanged,
  type User,
} from 'firebase/auth';
import {
  getMessaging,
  getToken,
  onMessage,
  isSupported,
  Messaging,
  MessagePayload,
} from 'firebase/messaging';
import type { WebAuthUser } from './auth-types';
import { getBetterAuthToken, isBetterAuthEnabled } from './better-auth';

// Firebase configuration from environment variables
const firebaseConfig = {
  apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY,
  authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
  projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
  storageBucket: process.env.NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET,
  messagingSenderId: process.env.NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID,
  appId: process.env.NEXT_PUBLIC_FIREBASE_APP_ID,
  measurementId: process.env.NEXT_PUBLIC_FIREBASE_MEASUREMENT_ID,
};

const hasFirebaseConfig = [
  firebaseConfig.apiKey,
  firebaseConfig.authDomain,
  firebaseConfig.projectId,
  firebaseConfig.storageBucket,
  firebaseConfig.messagingSenderId,
  firebaseConfig.appId,
].every((value) => typeof value === 'string' && value.trim().length > 0);

// vinext evaluates client component dependency graphs in the Worker RSC
// runtime. Firebase Auth is browser-only, so never call getAuth() while
// rendering on the server. Keep the exported shape stable for client modules
// that inspect auth.currentUser/app, while all real auth operations remain
// browser-only.
const isBrowser = typeof window !== 'undefined';
const app =
  isBrowser && hasFirebaseConfig && process.env.NEXT_PUBLIC_AUTH_MODE !== 'better-auth'
    ? getApps().length === 0
      ? initializeApp(firebaseConfig)
      : getApps()[0]
    : null;

const serverSafeAuth = {
  currentUser: null,
  app: { options: firebaseConfig },
} as unknown as Auth;

// Initialize Firebase Auth only in the browser; the server-safe value is never
// used for an auth operation because those operations are triggered by client
// effects or user gestures.
export const auth = app ? getAuth(app) : serverSafeAuth;

let compatUser: WebAuthUser | null = null;

export function setCompatCurrentUser(user: WebAuthUser | null): void {
  compatUser = user;
}

export function getCompatCurrentUser(): WebAuthUser | null {
  return compatUser || (auth.currentUser as WebAuthUser | null);
}

// Auth providers are browser-only too. Keeping them null in the Worker RSC
// runtime avoids pulling Firebase's popup implementation into server render.
const googleProvider = app ? new GoogleAuthProvider() : null;
googleProvider?.setCustomParameters({
  prompt: 'select_account',
});

const appleProvider = app ? new OAuthProvider('apple.com') : null;
appleProvider?.addScope('email');
appleProvider?.addScope('name');

/**
 * Sign in with Google
 */
export const signInWithGoogle = async (): Promise<User | null> => {
  if (!app || !googleProvider)
    throw new Error('Firebase authentication is not configured');
  try {
    const result = await signInWithPopup(auth, googleProvider);
    return result.user;
  } catch (error) {
    console.error('Google sign-in error:', error);
    throw error;
  }
};

/**
 * Sign in with Apple
 */
export const signInWithApple = async (): Promise<User | null> => {
  if (!app || !appleProvider)
    throw new Error('Firebase authentication is not configured');
  try {
    const result = await signInWithPopup(auth, appleProvider);
    return result.user;
  } catch (error) {
    console.error('Apple sign-in error:', error);
    throw error;
  }
};

/**
 * Sign out the current user
 */
export const signOutUser = async (): Promise<void> => {
  if (!app) {
    setCompatCurrentUser(null);
    return;
  }
  try {
    await signOut(auth);
  } catch (error) {
    console.error('Sign out error:', error);
    throw error;
  }
};

/**
 * Get the current user's ID token for API calls
 * Always call this fresh before API requests (don't cache)
 */
export const getIdToken = async (): Promise<string | null> => {
  if (isBetterAuthEnabled) return getBetterAuthToken();
  const user = auth.currentUser;
  if (!user) return null;

  try {
    // Force refresh if token is expired
    const token = await user.getIdToken();
    return token;
  } catch (error) {
    console.error('Get ID token error:', error);
    return null;
  }
};

/**
 * Subscribe to auth state changes
 */
export const onAuthStateChange = (callback: (user: WebAuthUser | null) => void) => {
  if (!app) {
    callback(null);
    return () => undefined;
  }
  return onAuthStateChanged(auth, (user) => {
    const normalized = user as WebAuthUser | null;
    setCompatCurrentUser(normalized);
    callback(normalized);
  });
};

// ============================================
// Firebase Cloud Messaging (FCM) for Push Notifications
// ============================================

// VAPID key for web push
const VAPID_KEY = process.env.NEXT_PUBLIC_FIREBASE_VAPID_KEY;

// Cached messaging instance
let messagingInstance: Messaging | null = null;

/**
 * Check if the browser supports Firebase Cloud Messaging
 */
export const isMessagingSupported = async (): Promise<boolean> => {
  if (typeof window === 'undefined') return false;

  try {
    return await isSupported();
  } catch {
    return false;
  }
};

/**
 * Get the Firebase Messaging instance (lazy initialization)
 * Returns null if messaging is not supported
 */
export const getMessagingInstance = async (): Promise<Messaging | null> => {
  if (typeof window === 'undefined' || !app) return null;

  if (messagingInstance) return messagingInstance;

  const supported = await isMessagingSupported();
  if (!supported) {
    console.warn('Firebase Messaging is not supported in this browser');
    return null;
  }

  try {
    messagingInstance = getMessaging(app);
    return messagingInstance;
  } catch (error) {
    console.error('Failed to initialize Firebase Messaging:', error);
    return null;
  }
};

/**
 * Register the service worker for FCM and wait for it to be active
 */
const registerServiceWorker = async (): Promise<ServiceWorkerRegistration | null> => {
  if (typeof window === 'undefined' || !('serviceWorker' in navigator)) {
    return null;
  }

  try {
    const registration = await navigator.serviceWorker.register(
      '/firebase-messaging-sw.js',
    );

    // Wait for the service worker to be active
    const installingWorker = registration.installing;
    if (installingWorker) {
      await new Promise<void>((resolve) => {
        const handler = (e: Event) => {
          if ((e.target as ServiceWorker).state === 'activated') {
            installingWorker.removeEventListener('statechange', handler);
            resolve();
          }
        };
        installingWorker.addEventListener('statechange', handler);
      });
    } else {
      const waitingWorker = registration.waiting;
      if (waitingWorker) {
        await new Promise<void>((resolve) => {
          const handler = (e: Event) => {
            if ((e.target as ServiceWorker).state === 'activated') {
              waitingWorker.removeEventListener('statechange', handler);
              resolve();
            }
          };
          waitingWorker.addEventListener('statechange', handler);
        });
      }
    }

    // Also ensure the service worker is ready
    await navigator.serviceWorker.ready;

    return registration;
  } catch (error) {
    console.error('Service Worker registration failed:', error);
    return null;
  }
};

/**
 * Request notification permission and get FCM token
 * @returns The FCM token if permission granted, null otherwise
 */
export const requestNotificationPermission = async (): Promise<string | null> => {
  if (typeof window === 'undefined') return null;

  // Check if notifications are supported
  if (!('Notification' in window)) {
    console.warn('This browser does not support notifications');
    return null;
  }

  // Check if service workers are supported
  if (!('serviceWorker' in navigator)) {
    console.warn('Service workers are not supported');
    return null;
  }

  // Register service worker FIRST (before calling getMessaging)
  const swRegistration = await registerServiceWorker();
  if (!swRegistration) return null;

  // Request permission
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') {
    return null;
  }

  // Now get messaging instance (after SW is registered)
  const messaging = await getMessagingInstance();
  if (!messaging) return null;

  // Get FCM token
  try {
    const token = await getToken(messaging, {
      vapidKey: VAPID_KEY,
      serviceWorkerRegistration: swRegistration,
    });

    if (token) {
      return token;
    } else {
      return null;
    }
  } catch (error) {
    console.error('Failed to get FCM token:', error);
    return null;
  }
};

/**
 * Get the current FCM token without requesting permission
 * Useful for checking if we already have a valid token
 */
export const getCurrentFCMToken = async (): Promise<string | null> => {
  if (typeof window === 'undefined') return null;

  // Check current permission status
  if (Notification.permission !== 'granted') {
    return null;
  }

  // Check if service workers are supported
  if (!('serviceWorker' in navigator)) {
    return null;
  }

  // Register service worker FIRST
  const swRegistration = await registerServiceWorker();
  if (!swRegistration) return null;

  // Then get messaging instance
  const messaging = await getMessagingInstance();
  if (!messaging) return null;

  try {
    const token = await getToken(messaging, {
      vapidKey: VAPID_KEY,
      serviceWorkerRegistration: swRegistration,
    });
    return token || null;
  } catch (error) {
    console.error('Failed to get current FCM token:', error);
    return null;
  }
};

/**
 * Subscribe to foreground messages
 * These are messages received while the app is in focus
 * @param callback Function to call when a message is received
 * @returns Unsubscribe function
 */
export const onForegroundMessage = async (
  callback: (payload: MessagePayload) => void,
): Promise<(() => void) | null> => {
  const messaging = await getMessagingInstance();
  if (!messaging) {
    return null;
  }

  return onMessage(messaging, (payload) => {
    callback(payload);
  });
};

/**
 * Get the current notification permission status
 */
export const getNotificationPermission = (): NotificationPermission | 'unsupported' => {
  if (typeof window === 'undefined' || !('Notification' in window)) {
    return 'unsupported';
  }
  return Notification.permission;
};

export default app;
