import XCTest

@testable import Omi_Computer

#if DEBUG
  // omi-release-compile: DEBUG-only test seams; release bundle must compile without them.

  @MainActor
  final class AuthExternalTokenInjectionTests: XCTestCase {
    private var priorExternalToken: String?

    override func setUp() async throws {
      clearAuthDefaults()
      priorExternalToken = getenv("OMI_AUTH_API_TOKEN").flatMap { String(validatingCString: $0) }
      unsetenv("OMI_AUTH_API_TOKEN")
    }

    override func tearDown() async throws {
      if let priorExternalToken {
        setenv("OMI_AUTH_API_TOKEN", priorExternalToken, 1)
      } else {
        unsetenv("OMI_AUTH_API_TOKEN")
      }
      clearAuthDefaults()
    }

    private func clearAuthDefaults() {
      // `clearTokens()` deletes the real, unsandboxed macOS Keychain item AuthService
      // uses for auth tokens (see DesktopKeychainStore) — not just UserDefaults. That
      // item is process-wide, not instance-scoped, so any AuthService instance can
      // remove what another instance wrote (testExternalTokenWinsOverStoredToken saves
      // through one). Skipping this left a stale userId "u1" Keychain entry for
      // testExternalTokenIgnoredWhenEnvEmpty to read when CI's isolation fallback
      // reran this suite through the same worker runtime after a batch failure
      // elsewhere — UserDefaults gets a fresh CFFIXED_USER_HOME per worker, but the
      // Keychain does not.
      AuthService().clearTokens()
      UserDefaults.standard.removeObject(forKey: .authUserId)
    }

    private func makeAuth() -> AuthService {
      AuthService()
    }

    func testExternalTokenInjectedWhenEnvSet() async throws {
      // Even with no signed-in Firebase session, an injected external JWT
      // must be returned as the idToken (self-hosted backend auth).
      setenv("OMI_AUTH_API_TOKEN", "better-auth-jwt-abc123", 1)
      defer { unsetenv("OMI_AUTH_API_TOKEN") }

      let auth = makeAuth()
      let token = try await auth.getIdToken()
      XCTAssertEqual(token, "better-auth-jwt-abc123")
    }

    func testExternalTokenIgnoredWhenEnvEmpty() async throws {
      // Empty env must fall through to the normal (Firebase) token path, not
      // return an empty token.
      unsetenv("OMI_AUTH_API_TOKEN")

      let auth = makeAuth()
      // No signed-in session and no injected token: must throw notSignedIn
      // rather than return an empty bearer.
      do {
        _ = try await auth.getIdToken()
        XCTFail("expected notSignedIn when no session and no injected token")
      } catch {
        // expected: AuthService throws because there is no session
      }
    }

    func testExternalTokenWinsOverStoredToken() async throws {
      setenv("OMI_AUTH_API_TOKEN", "injected-jwt-xyz", 1)
      defer { unsetenv("OMI_AUTH_API_TOKEN") }

      let auth = makeAuth()
      // Store a Firebase-ish idToken to prove the injected one wins.
      try auth.saveTokens(
        idToken: "firebase-token",
        refreshToken: "refresh",
        expiresIn: 3600,
        userId: "u1")

      let token = try await auth.getIdToken()
      XCTAssertEqual(token, "injected-jwt-xyz")
    }

    /// Regression test for a stale Keychain token leaking across separate
    /// `AuthService` instances (and, in CI, across separate SwiftPM test-process
    /// invocations of this suite that share a worker runtime — see
    /// `clearAuthDefaults()`). The Keychain item AuthService reads/writes is
    /// process-wide, not instance-scoped, so a second instance must see a clean
    /// signed-out state once the first instance's tokens are cleared.
    func testStoredTokenDoesNotLeakAcrossAuthServiceInstances() async throws {
      unsetenv("OMI_AUTH_API_TOKEN")

      let first = makeAuth()
      try first.saveTokens(
        idToken: "leaked-token",
        refreshToken: "refresh",
        expiresIn: 3600,
        userId: "u1")
      first.clearTokens()

      let second = makeAuth()
      do {
        _ = try await second.getIdToken()
        XCTFail("expected notSignedIn: clearTokens() should have removed the Keychain-backed session")
      } catch {
        // expected: no session survives clearTokens()
      }
    }
  }
#endif
