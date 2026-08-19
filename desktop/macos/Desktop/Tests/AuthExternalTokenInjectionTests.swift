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
      UserDefaults.standard.removeObject(forKey: .authIdToken)
      UserDefaults.standard.removeObject(forKey: .authRefreshToken)
      UserDefaults.standard.removeObject(forKey: .authTokenExpiry)
      UserDefaults.standard.removeObject(forKey: .authTokenUserId)
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
  }
#endif
