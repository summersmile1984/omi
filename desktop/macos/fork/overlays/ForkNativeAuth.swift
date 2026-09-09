import Foundation

@MainActor
final class ForkNativeAuthState {
  let client = NativeAuthClient(profile: ForkDesktopBuild.profile)
  let store: NativeSessionStore

  init() {
    do {
      store = try NativeSessionStore(
        prefix: ForkDesktopBuild.keychainPrefix,
        team: NativeSigningIdentity.current(),
        bundle: AppBuild.bundleIdentifier,
        profile: ForkDesktopBuild.profile,
        operations: .init(
          read: { service, account in
            switch DesktopKeychainStore.readString(service: service, account: account) {
            case .found(let value): return value
            case .missing: return nil
            case .unavailable: throw NativeAuthFailure.storageUnavailable
            }
          },
          write: { value, service, account in
            guard DesktopKeychainStore.setString(value, service: service, account: account) else {
              throw NativeAuthFailure.storageUnavailable
            }
          },
          remove: { service, account in
            DesktopKeychainStore.delete(service: service, account: account)
            guard case .missing = DesktopKeychainStore.readString(service: service, account: account) else {
              throw NativeAuthFailure.storageUnavailable
            }
          }
        )
      )
    } catch {
      fatalError("Invalid native authentication storage configuration")
    }
  }
}

extension AuthService {
  func signInWithEmail(email: String, password: String, name: String? = nil) async throws {
    let attempt = beginSessionAttempt()
    AuthState.shared.isLoading = true
    AuthState.shared.error = nil
    defer {
      if isSessionAttemptCurrent(attempt) { AuthState.shared.isLoading = false }
    }
    let session: NativeAuthSession
    if let name {
      session = try await forkNativeState.client.signUp(name: name, email: email, password: password)
    } else {
      session = try await forkNativeState.client.signIn(email: email, password: password)
    }
    guard try await forkCommitSession(session, attempt: attempt) else { throw NativeAuthFailure.superseded }
  }

  func forkCommitSession(_ session: NativeAuthSession, attempt: AuthSessionAttempt) async throws -> Bool {
    let fence = sessionAttemptFence
    let committed = try await RuntimeOwnerIdentity.performEffectiveOwnerTransition(
      plannedNextOwner: { _, previous in fence.isCurrent(attempt) ? session.user.id : previous },
      { _ in
        try await MainActor.run {
          try fence.commitIfCurrent(attempt) {
            try self.forkNativeState.store.save(session)
            self.forkNativeState.client.invalidateAccessCache()
            let defaults = UserDefaults.standard
            defaults.set(true, forKey: .authIsSignedIn)
            defaults.set(session.user.id, forKey: .authUserId)
            defaults.set(session.user.email, forKey: .authUserEmail)
            defaults.set(session.user.name ?? "", forKey: .authGivenName)
            defaults.set("", forKey: .authFamilyName)
            defaults.synchronize()
            return true
          } ?? false
        }
      }
    )
    guard committed, isSessionAttemptCurrent(attempt) else { return false }
    AuthState.shared.userEmail = session.user.email
    AuthSessionCoordinator.shared.resetAfterSuccessfulSignIn()
    postNameDidUpdate()
    return true
  }

  func forkRestore(attempt: AuthSessionAttempt) async {
    AuthState.shared.transition(to: .restoring)
    do {
      guard let stored = try forkNativeState.store.read() else {
        _ = try await commitSignedOutSession(attempt: attempt, phase: .signedOut)
        return
      }
      let restored = try await forkNativeState.client.restore(stored)
      _ = try await forkCommitSession(restored, attempt: attempt)
    } catch NativeAuthFailure.unauthorized {
      guard isSessionAttemptCurrent(attempt) else { return }
      await invalidateSession(reason: .restoredSessionInvalid)
    } catch {
      guard isSessionAttemptCurrent(attempt) else { return }
      AuthState.shared.error = error.localizedDescription
      AuthState.shared.transition(to: .recoveryRequired)
    }
  }

  func forkAccessToken(forceRefresh: Bool) async throws -> String {
    let attempt = currentSessionAttempt()
    guard let session = try forkNativeState.store.read(),
      session.user.id == UserDefaults.standard.string(forKey: .authUserId)
    else { throw AuthError.notSignedIn }
    do {
      let token = try await forkNativeState.client.accessToken(for: session, forceRefresh: forceRefresh)
      guard isSessionAttemptCurrent(attempt) else { throw AuthError.userChangedDuringRequest }
      return token
    } catch NativeAuthFailure.unauthorized {
      guard isSessionAttemptCurrent(attempt) else { throw AuthError.userChangedDuringRequest }
      await invalidateSession(reason: .definitiveRefreshFailure)
      throw AuthError.notSignedIn
    }
  }

  func forkInvalidate() async -> Bool {
    let attempt = beginSessionAttempt()
    do {
      return try await commitSignedOutSession(
        attempt: attempt, phase: .needsReauth,
        beforeClearingCredentials: { try self.forkNativeState.store.clear() }
      )
    } catch {
      guard isSessionAttemptCurrent(attempt) else { return false }
      AuthState.shared.error = error.localizedDescription
      AuthState.shared.transition(to: .recoveryRequired)
      return false
    }
  }

  func forkRevoke(attempt: AuthSessionAttempt) async throws {
    if let session = try forkNativeState.store.read() {
      try await forkNativeState.client.revoke(session)
    }
    guard isSessionAttemptCurrent(attempt) else { throw NativeAuthFailure.superseded }
  }
}
