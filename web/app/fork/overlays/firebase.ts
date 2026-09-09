import type { MessagePayload } from 'firebase/messaging';
import { authSession, type AppUser } from '@/lib/fork/auth-session';

// This is the build adapter for the existing Web authentication port. All
// relative and @/lib/firebase imports resolve to this one file in fork builds.
export const auth = {
  get currentUser() {
    return authSession().currentUser;
  },
};
export const getIdToken = () => authSession().getToken();
export const signOutUser = () => authSession().signOut();
export const onAuthStateChange = (listener: (user: AppUser | null) => void) =>
  authSession().subscribe(listener);

// webhook is an operator capability, not an implementation of browser FCM.
// The fork never initializes Firebase, registers its worker, or requests a token.
export const isMessagingSupported = async () => false;
export const getMessagingInstance = async () => null;
export const requestNotificationPermission = async () => null;
export const getCurrentFCMToken = async () => null;
export const onForegroundMessage = async (
  _listener: (message: MessagePayload) => void,
): Promise<(() => void) | null> => null;
export const getNotificationPermission = (): NotificationPermission | 'unsupported' =>
  'unsupported';
