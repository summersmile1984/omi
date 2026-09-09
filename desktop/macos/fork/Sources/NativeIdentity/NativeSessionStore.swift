import CryptoKit
import Foundation

/// Storage delegates to the existing desktop silent Keychain primitive. The
/// opaque-session account never reads Firebase accounts or legacy service names.
@MainActor
public final class NativeSessionStore {
  public struct Operations {
    public let read: (String, String) throws -> String?
    public let write: (String, String, String) throws -> Void
    public let remove: (String, String) throws -> Void

    public init(
      read: @escaping (String, String) throws -> String?,
      write: @escaping (String, String, String) throws -> Void,
      remove: @escaping (String, String) throws -> Void
    ) {
      self.read = read
      self.write = write
      self.remove = remove
    }
  }

  public let service: String
  public let account = "better-auth-opaque-session-v1"
  private let operations: Operations
  private var cached: NativeAuthSession?
  private var loaded = false

  public init(
    prefix: String, team: String, bundle: String, profile: NativeDeploymentProfile,
    operations: Operations
  ) throws {
    guard !prefix.isEmpty, !team.isEmpty, !bundle.isEmpty else {
      throw NativeAuthFailure.invalidConfiguration
    }
    let authority = SHA256.hash(data: Data(profile.authBaseURL.absoluteString.utf8))
      .map { String(format: "%02x", $0) }.joined()
    service = "\(prefix)native-auth.v1.team.\(team).bundle.\(bundle).\(profile.name).\(authority)"
    self.operations = operations
  }

  public func read() throws -> NativeAuthSession? {
    if loaded { return cached }
    guard let encoded = try operations.read(service, account) else {
      loaded = true
      return nil
    }
    guard let session = try? JSONDecoder().decode(NativeAuthSession.self, from: Data(encoded.utf8)),
      !session.token.isEmpty, !session.user.id.isEmpty
    else { throw NativeAuthFailure.storageUnavailable }
    cached = session
    loaded = true
    return session
  }

  /// Call inside the app's synchronous credentials + durable owner transaction.
  public func save(_ session: NativeAuthSession) throws {
    let data = try JSONEncoder().encode(session)
    guard let encoded = String(data: data, encoding: .utf8) else {
      throw NativeAuthFailure.storageUnavailable
    }
    try operations.write(encoded, service, account)
    cached = session
    loaded = true
  }

  /// A Keychain failure leaves the in-memory credential intact for recovery.
  public func clear() throws {
    try operations.remove(service, account)
    cached = nil
    loaded = true
  }
}
