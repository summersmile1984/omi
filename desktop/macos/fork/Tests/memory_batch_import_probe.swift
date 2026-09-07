import Foundation

// Only the surrounding account/network dependencies are controlled. The test
// compiler receives the staged import enum, current wire models and encoder.
enum MemoryCategory: String { case system, manual }
struct RuntimeOwnerAuthorizationSnapshot { let generation: Int }
enum RuntimeOwnerIdentity {
  nonisolated(unsafe) static var current = true
  static func isAuthorizationCurrent(_ value: RuntimeOwnerAuthorizationSnapshot) -> Bool { current }
}
enum ForkDesktopBuild {
  struct Profile { var target: String }
  nonisolated(unsafe) static var profile = Profile(target: "cloudflare")
}
enum AuthError: Error { case userChangedDuringRequest }
enum APIError: Error {
  case httpError(statusCode: Int, detail: String)
  case invalidResponse
}
func log(_ message: String) {}

protocol MemoryBatchCreating {
  func createMemoriesBatch(
    _ memories: [MemoryBatchItem], authorizationSnapshot: RuntimeOwnerAuthorizationSnapshot?
  ) async throws -> BatchMemoriesResponse
}
actor APIClient: MemoryBatchCreating {
  static let shared = APIClient()
  static let memoriesBatchMaxSize = 100
  var calls: [[MemoryBatchItem]] = []
  let failCall: Int?
  let switchOwner: Bool
  init(failCall: Int? = nil, switchOwner: Bool = false) {
    self.failCall = failCall
    self.switchOwner = switchOwner
  }
  func createMemoriesBatch(
    _ memories: [MemoryBatchItem], authorizationSnapshot: RuntimeOwnerAuthorizationSnapshot?
  ) async throws -> BatchMemoriesResponse {
    struct Body: Encodable { let memories: [MemoryBatchItem] }
    let wire = try OmiHTTPTransport.makeEncoder().encode(Body(memories: memories))
    if ForkDesktopBuild.profile.target == "cloudflare" { precondition(wire.count <= 1_000_000) }
    precondition(memories.count <= 100)
    calls.append(memories)
    if switchOwner { RuntimeOwnerIdentity.current = false }
    if calls.count == failCall {
      throw APIError.httpError(statusCode: 400, detail: "synthetic rejection")
    }
    return BatchMemoriesResponse(memories: [], createdCount: memories.count)
  }
}

/* ACTUAL_PRODUCTION_ENCODER */
/* ACTUAL_PRODUCTION_MODELS */
/* ACTUAL_STAGED_IMPORT */

@main struct Probe {
  static func main() async throws {
    let input = (0..<100).map {
      MemoryBatchItem(
        content: String(repeating: "汉😀\"/", count: 5_000) + String($0), tags: ["metadata"],
        headline: "Headline")
    }
    let snapshot = RuntimeOwnerAuthorizationSnapshot(generation: 1)
    let api = APIClient()
    let result = await OnboardingMemoryBatchImportService.save(
      input, logPrefix: "fixture", authorizationSnapshot: snapshot, apiClient: api, sleep: { _ in })
    let calls = await api.calls
    precondition(result.saved == input.count && result.failed == 0 && calls.count > 1)
    precondition(calls.flatMap { $0 }.map(\.content) == input.map(\.content))

    let failureAPI = APIClient(failCall: 2)
    let failed = await OnboardingMemoryBatchImportService.save(
      input, logPrefix: "fixture", authorizationSnapshot: snapshot, apiClient: failureAPI,
      sleep: { _ in })
    let failureCalls = await failureAPI.calls
    precondition(failureCalls.count == calls.count)
    precondition(
      failed.failed == failureCalls[1].count && failed.saved == input.count - failed.failed)
    precondition(failureCalls.flatMap { $0 }.map(\.content) == input.map(\.content))

    let oversized = MemoryBatchItem(
      content: "oversized metadata", tags: [String(repeating: "x", count: 1_000_000)])
    let isolated = APIClient()
    let mixed = await OnboardingMemoryBatchImportService.save(
      [input[0], oversized, input[1]], logPrefix: "fixture", authorizationSnapshot: snapshot,
      apiClient: isolated, sleep: { _ in })
    precondition(mixed.saved == 2 && mixed.failed == 1)
    let isolatedCalls = await isolated.calls
    precondition(
      isolatedCalls.flatMap { $0 }.map(\.content) == [input[0].content, input[1].content])

    let accountAPI = APIClient(switchOwner: true)
    _ = await OnboardingMemoryBatchImportService.save(
      input, logPrefix: "fixture", authorizationSnapshot: snapshot, apiClient: accountAPI,
      sleep: { _ in })
    let accountCalls = await accountAPI.calls
    precondition(accountCalls.count == 1)
    RuntimeOwnerIdentity.current = true

    ForkDesktopBuild.profile.target = "self_hosted"
    let server = APIClient()
    let serverResult = await OnboardingMemoryBatchImportService.save(
      input, logPrefix: "fixture", authorizationSnapshot: snapshot, apiClient: server,
      sleep: { _ in })
    precondition(serverResult.saved == 100 && serverResult.failed == 0)
    let serverCalls = await server.calls
    precondition(serverCalls.count == 1)
    print(
      "staged memory import: bytes, order, partial failure, oversize isolation, owner fence and both targets passed"
    )
  }
}
