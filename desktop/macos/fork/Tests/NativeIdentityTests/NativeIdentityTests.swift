import Foundation
import NativeIdentity
import Testing

private func profile(_ target: String = "self_hosted", auth: String = "https://auth.fixture.invalid") throws
  -> NativeDeploymentProfile
{
  let value: [String: Any] = [
    "name": "\(target).production", "target": target, "stage": "production", "identity_provider": "better_auth",
    "api_base_url": "https://api.fixture.invalid/prefix", "auth_base_url": auth,
    "web_base_url": "https://web.fixture.invalid", "mcp_base_url": "https://mcp.fixture.invalid",
    "share_base_url": "https://share.fixture.invalid", "objects_base_url": "https://objects.fixture.invalid",
  ]
  return try NativeDeploymentProfile.decode(JSONSerialization.data(withJSONObject: value))
}

private actor TransportFixture {
  struct Reply: Sendable {
    let status: Int
    let data: Data
    let headers: [String: String]
    init(_ json: String, status: Int = 200, headers: [String: String] = [:]) {
      self.status = status
      data = Data(json.utf8)
      self.headers = headers
    }
  }
  private var replies: [Reply]
  private(set) var requests: [URLRequest] = []
  init(_ replies: [Reply]) { self.replies = replies }
  func send(_ request: URLRequest) throws -> (Data, HTTPURLResponse) {
    requests.append(request)
    guard !replies.isEmpty else { throw NativeAuthFailure.unavailable }
    let reply = replies.removeFirst()
    return (
      reply.data,
      HTTPURLResponse(url: request.url!, statusCode: reply.status, httpVersion: nil, headerFields: reply.headers)!
    )
  }
}

@Test(arguments: ["self_hosted", "cloudflare"])
@MainActor
func bothTargetsUseOpaqueSessionAndSelectedAuthOrigin(target: String) async throws {
  let fixture = TransportFixture([
    .init(#"{"user":{"id":"existing-user","name":"Fixture"}}"#, headers: ["set-auth-token": "opaque-session"]),
    .init(#"{"user":{"id":"existing-user","name":"Fixture"}}"#),
    .init(#"{"success":true}"#),
  ])
  let selected = try profile(target)
  let client = NativeAuthClient(profile: selected, transport: { try await fixture.send($0) })
  let signedIn = try await client.signIn(email: "fixture@example.invalid", password: "synthetic")
  #expect(signedIn.token == "opaque-session")
  #expect(try await client.restore(signedIn) == signedIn)
  try await client.revoke(signedIn)
  let requests = await fixture.requests
  #expect(requests.map { $0.url!.path } == ["/api/auth/sign-in/email", "/api/auth/get-session", "/api/auth/sign-out"])
  #expect(requests.allSatisfy { $0.url!.host == "auth.fixture.invalid" && !$0.httpShouldHandleCookies })
  #expect(requests[1].value(forHTTPHeaderField: "Authorization") == "Bearer opaque-session")
  #expect(try selected.realtimeURL(path: "/v4/listen").absoluteString == "wss://api.fixture.invalid/prefix/v4/listen")
}

@Test @MainActor
func authenticationRejectsIncompleteCredentialAndChangedRestoreOwner() async throws {
  let fixture = TransportFixture([
    .init(#"{"user":{"id":"existing-user"}}"#),
    .init(#"{"user":{"id":"another-user"}}"#),
  ])
  let client = NativeAuthClient(profile: try profile(), transport: { try await fixture.send($0) })
  await #expect(throws: NativeAuthFailure.invalidResponse) {
    try await client.signUp(name: "Fixture", email: "fixture@example.invalid", password: "synthetic")
  }
  await #expect(throws: NativeAuthFailure.unauthorized) {
    try await client.restore(NativeAuthSession(token: "opaque", user: .init(id: "existing-user")))
  }
}

@Test @MainActor
func revokeOutageIsRetryableButAlreadyRevokedIsSuccessful() async throws {
  let fixture = TransportFixture([.init("{}", status: 503), .init("{}", status: 401)])
  let client = NativeAuthClient(profile: try profile(), transport: { try await fixture.send($0) })
  let session = NativeAuthSession(token: "opaque", user: .init(id: "existing-user"))
  await #expect(throws: NativeAuthFailure.unavailable) { try await client.revoke(session) }
  try await client.revoke(session)
}

private func token(subject: String = "existing-user", lifetime: Int = 3600) throws -> String {
  let claims: [String: Any] = [
    "uid": subject, "sub": subject, "sid": "session-id", "iat": 1_800_000_000, "exp": 1_800_000_000 + lifetime,
  ]
  return "header." + (try JSONSerialization.data(withJSONObject: claims)).base64EncodedString() + ".signature"
}

@Test @MainActor
func accessJWTIsSeparateCachedAndOwnerBound() async throws {
  let jwt = try token()
  let fixture = TransportFixture([
    .init("{\"token\":\"\(jwt)\"}"),
    .init("{\"token\":\"\(try token(subject: "wrong-owner"))\"}"),
    .init("{}", status: 503),
  ])
  let client = NativeAuthClient(
    profile: try profile(), transport: { try await fixture.send($0) },
    now: { Date(timeIntervalSince1970: 1_800_000_000) }
  )
  let session = NativeAuthSession(token: "opaque", user: .init(id: "existing-user"))
  #expect(try await client.accessToken(for: session) == jwt)
  #expect(try await client.accessToken(for: session) == jwt)
  #expect(await fixture.requests.count == 1)
  await #expect(throws: NativeAuthFailure.invalidResponse) {
    try await client.accessToken(for: session, forceRefresh: true)
  }
  await #expect(throws: NativeAuthFailure.unavailable) {
    try await client.accessToken(for: session, forceRefresh: true)
  }
  #expect(try await client.accessToken(for: session) == jwt)
  #expect(await fixture.requests.last?.value(forHTTPHeaderField: "Authorization") == "Bearer opaque")
}

@Test @MainActor
func keychainScopeAndWriteFailuresPreserveTheAuthoritativeCredential() throws {
  var items: [String: String] = [:]
  var failWrites = false
  let operations = NativeSessionStore.Operations(
    read: { service, account in items[service + account] },
    write: { value, service, account in
      if failWrites { throw NativeAuthFailure.storageUnavailable }
      items[service + account] = value
    },
    remove: { service, account in items.removeValue(forKey: service + account) }
  )
  let selected = try profile()
  let store = try NativeSessionStore(
    prefix: "com.fixture.", team: "adhoc", bundle: "com.fixture.omi-auth", profile: selected, operations: operations)
  let existing = NativeAuthSession(token: "opaque-original", user: .init(id: "existing-user"))
  try store.save(existing)
  #expect(!items.values.first!.contains(".signature"))
  failWrites = true
  #expect(throws: NativeAuthFailure.storageUnavailable) {
    try store.save(NativeAuthSession(token: "opaque-new", user: .init(id: "new-user")))
  }
  #expect(try store.read() == existing)
  var services: Set<String> = [store.service]
  for (team, bundle, deployment) in [
    ("signed-team", "com.fixture.omi-auth", selected),
    ("adhoc", "com.fixture.omi-other", selected),
    ("adhoc", "com.fixture.omi-auth", try profile("cloudflare")),
    ("adhoc", "com.fixture.omi-auth", try profile(auth: "https://other.fixture.invalid")),
  ] {
    let other = try NativeSessionStore(
      prefix: "com.fixture.", team: team, bundle: bundle, profile: deployment, operations: operations)
    services.insert(other.service)
    #expect(try other.read() == nil)
  }
  #expect(services.count == 5)
}

@Test
func profileNeverSilentlySelectsAnUpstreamOrMalformedAuthority() throws {
  #expect(throws: (any Error).self) { try profile("omi_cloud") }
  #expect(throws: (any Error).self) { try profile(auth: "http://auth.fixture.invalid") }
  #expect(throws: (any Error).self) { try profile(auth: "https://auth.fixture.invalid/hidden-path") }
}

private actor DeferredTokenFixture {
  private var response: CheckedContinuation<(Data, HTTPURLResponse), any Error>?
  private var waiting: CheckedContinuation<Void, Never>?
  private(set) var calls = 0
  func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
    calls += 1
    return try await withCheckedThrowingContinuation { continuation in
      response = continuation
      waiting?.resume()
      waiting = nil
    }
  }
  func waitUntilRequested() async {
    if response != nil { return }
    await withCheckedContinuation { waiting = $0 }
  }
  func reply(_ token: String) {
    let continuation = response!
    response = nil
    continuation.resume(
      returning: (
        Data("{\"token\":\"\(token)\"}".utf8),
        HTTPURLResponse(
          url: URL(string: "https://auth.fixture.invalid/api/auth/token")!, statusCode: 200, httpVersion: nil,
          headerFields: [:])!
      ))
  }
}

@Test @MainActor
func lateExchangeCannotResurrectAnInvalidatedAccessCache() async throws {
  let fixture = DeferredTokenFixture()
  let jwt = try token()
  let client = NativeAuthClient(
    profile: try profile(), transport: { try await fixture.send($0) },
    now: { Date(timeIntervalSince1970: 1_800_000_000) })
  let session = NativeAuthSession(token: "opaque", user: .init(id: "existing-user"))
  let old = Task { try await client.accessToken(for: session) }
  await fixture.waitUntilRequested()
  client.invalidateAccessCache()
  await fixture.reply(jwt)
  await #expect(throws: NativeAuthFailure.superseded) { try await old.value }
  let fresh = Task { try await client.accessToken(for: session) }
  await fixture.waitUntilRequested()
  #expect(await fixture.calls == 2)
  await fixture.reply(jwt)
  #expect(try await fresh.value == jwt)
}

@Test
func adhocRebuildsCannotQueryAnEarlierArtifactsKeychainACL() throws {
  let oldCode = Data([0x01, 0x02])
  let newCode = Data([0x03, 0x04])
  #expect(
    try NativeSigningIdentity.scope(team: nil, uniqueCode: oldCode)
      != NativeSigningIdentity.scope(team: nil, uniqueCode: newCode))
  #expect(
    try NativeSigningIdentity.scope(team: nil, uniqueCode: oldCode)
      == NativeSigningIdentity.scope(team: nil, uniqueCode: oldCode))
  #expect(
    try NativeSigningIdentity.scope(team: "SYNTHETIC", uniqueCode: oldCode)
      == NativeSigningIdentity.scope(team: "SYNTHETIC", uniqueCode: newCode))
  #expect(throws: NativeAuthFailure.invalidConfiguration) {
    try NativeSigningIdentity.scope(team: nil, uniqueCode: nil)
  }
}

@Test @MainActor
func missingSessionIsDefinitiveButMalformedRestoreIsRecoverable() async throws {
  let fixture = TransportFixture([.init("null"), .init("not-json")])
  let client = NativeAuthClient(profile: try profile(), transport: { try await fixture.send($0) })
  let session = NativeAuthSession(token: "old-opaque", user: .init(id: "existing-user"))
  await #expect(throws: NativeAuthFailure.unauthorized) { try await client.restore(session) }
  await #expect(throws: NativeAuthFailure.invalidResponse) { try await client.restore(session) }
}

@Test @MainActor
func wrongPasswordIsRejectedInputRatherThanAnExpiredSession() async throws {
  let fixture = TransportFixture([.init("{}", status: 401)])
  let client = NativeAuthClient(profile: try profile(), transport: { try await fixture.send($0) })
  await #expect(throws: NativeAuthFailure.rejected(401)) {
    try await client.signIn(email: "fixture@example.invalid", password: "wrong-synthetic")
  }
}

@Test @MainActor
func extremeNegativeIssuedAtFailsWithoutIntegerOverflow() async throws {
  let payload = try JSONSerialization.data(withJSONObject: [
    "uid": "existing-user", "sub": "existing-user", "sid": "session-id",
    "iat": Int.min, "exp": 1_800_000_300,
  ])
  let malformed = "header." + payload.base64EncodedString() + ".signature"
  let fixture = TransportFixture([.init("{\"token\":\"\(malformed)\"}")])
  let client = NativeAuthClient(
    profile: try profile(), transport: { try await fixture.send($0) },
    now: { Date(timeIntervalSince1970: 1_800_000_000) })
  let session = NativeAuthSession(token: "opaque", user: .init(id: "existing-user"))
  await #expect(throws: NativeAuthFailure.invalidResponse) { try await client.accessToken(for: session) }
}

@Test @MainActor
func issuedAtSkewAllowsSixtySecondsWithoutExtendingExpiration() async throws {
  let now = 1_800_000_000
  for (offset, lifetime, accepted) in [(30, 300, true), (60, 300, true), (61, 300, false), (-3600, 3600, false)] {
    let payload = try JSONSerialization.data(withJSONObject: [
      "uid": "existing-user", "sub": "existing-user", "sid": "session-id",
      "iat": now + offset, "exp": now + offset + lifetime,
    ])
    let jwt = "header." + payload.base64EncodedString() + ".signature"
    let fixture = TransportFixture([.init("{\"token\":\"\(jwt)\"}")])
    let client = NativeAuthClient(
      profile: try profile(), transport: { try await fixture.send($0) },
      now: { Date(timeIntervalSince1970: TimeInterval(now)) })
    let session = NativeAuthSession(token: "opaque", user: .init(id: "existing-user"))
    if accepted {
      #expect(try await client.accessToken(for: session) == jwt)
    } else {
      await #expect(throws: NativeAuthFailure.invalidResponse) { try await client.accessToken(for: session) }
    }
  }
}
