import Foundation

/// The existing authenticated local bridge calls production auth and native
/// transcription paths. Only the named local artifact includes this action.
/// This never captures a microphone or returns session/JWT material.
@MainActor
enum ForkNativeVerification {
  static func register() {
    #if DEBUG
      guard AppBuild.isNamedDevelopmentBundle, ForkDesktopBuild.profile.stage == "local" else { return }
      DesktopAutomationActionRegistry.shared.register(
        name: "fork_native_identity",
        summary: "Named local Better Auth verification; synthetic PCM only, no credential output",
        params: ["operation"]
      ) { params in
        switch params["operation"] {
        case "refresh": return try await refresh()
        case "transcription": return try await transcription()
        case "signout": return try await signOut()
        default: return ["error": "Select refresh, transcription, or signout"]
        }
      }
    #endif
  }

  private static func apiStatus(_ jwt: String) async throws -> Int {
    var request = URLRequest(url: ForkDesktopBuild.profile.apiBaseURL.appendingPathComponent("v1/action-items"))
    request.setValue("Bearer \(jwt)", forHTTPHeaderField: "Authorization")
    let (_, response) = try await NativeAuthClient.liveTransport()(request)
    return response.statusCode
  }

  static func refresh() async throws -> [String: String] {
    let auth = AuthService.shared
    let first = try await auth.getIdToken(forceRefresh: true)
    let cached = try await auth.getIdToken()
    let refreshed = try await auth.getIdToken(forceRefresh: true)
    return [
      "provider": "better_auth", "profile": ForkDesktopBuild.profile.name,
      "cached_token_reused": first == cached ? "true" : "false",
      "first_api_status": String(try await apiStatus(first)),
      "refreshed_api_status": String(try await apiStatus(refreshed)),
      "session_stored": (try auth.forkNativeState.store.read()) == nil ? "false" : "true",
    ]
  }

  static func signOut() async throws -> [String: String] {
    let auth = AuthService.shared
    let previousJWT = try await auth.getIdToken()
    try await auth.signOut()
    return [
      "signed_out": AuthState.shared.isSignedIn ? "false" : "true",
      "session_cleared": (try auth.forkNativeState.store.read()) == nil ? "true" : "false",
      "previous_jwt_api_status": String(try await apiStatus(previousJWT)),
    ]
  }

  static func transcription() async throws -> [String: String] {
    let service = try TranscriptionService(language: "en", mode: .conversation)
    let result = StreamResult()
    service.start(
      onSegments: { segments in result.addSegments(segments.count) },
      onEvent: { _ in }, onError: { _ in result.failed() },
      onConnected: {
        result.connected()
        // Explicit synthetic 100 ms PCM frame; never opens any capture device.
        service.sendAudio(Data(repeating: 0, count: 3200))
      }
    )
    defer { service.stop(discardBufferedAudio: true) }
    let deadline = Date().addingTimeInterval(20)
    while Date() < deadline {
      let snapshot = result.snapshot()
      if snapshot.segments > 0 || snapshot.failed { break }
      try await Task.sleep(for: .milliseconds(100))
    }
    let snapshot = result.snapshot()
    return [
      "connected": snapshot.connected ? "true" : "false", "segments": String(snapshot.segments),
      "failed": snapshot.failed ? "true" : "false", "endpoint": "/v4/listen",
      "synthetic_pcm_bytes": snapshot.connected ? "3200" : "0",
    ]
  }
}

private final class StreamResult: @unchecked Sendable {
  struct Snapshot {
    var connected = false
    var failed = false
    var segments = 0
  }
  private let lock = NSLock()
  private var value = Snapshot()
  func connected() { lock.withLock { value.connected = true } }
  func failed() { lock.withLock { value.failed = true } }
  func addSegments(_ count: Int) { lock.withLock { value.segments += count } }
  func snapshot() -> Snapshot { lock.withLock { value } }
}
