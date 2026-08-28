/**
 * The small user surface shared by Firebase and Better Auth adapters.
 *
 * Keeping this type independent from either provider lets the web UI run in a
 * Cloudflare Worker build without importing a browser-only Firebase Auth user
 * into the server/RSC graph.
 */
export interface WebAuthUser {
  uid: string;
  displayName: string | null;
  email: string | null;
  photoURL: string | null;
  getIdToken?: () => Promise<string | null>;
}
