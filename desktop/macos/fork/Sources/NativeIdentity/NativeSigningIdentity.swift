import Foundation
import Security

/// A developer team is stable across its authorized signed updates. Ad-hoc
/// signatures have no such authority: their unique code-directory hash must be
/// part of the Keychain namespace, or rebuilt code can prompt on a foreign ACL.
public enum NativeSigningIdentity {
  public static func scope(team: String?, uniqueCode: Data?) throws -> String {
    if let team, !team.isEmpty { return "team.\(team)" }
    guard let uniqueCode, !uniqueCode.isEmpty else { throw NativeAuthFailure.invalidConfiguration }
    return "adhoc." + uniqueCode.map { String(format: "%02x", $0) }.joined()
  }

  public static func current() throws -> String {
    var code: SecCode?
    guard SecCodeCopySelf([], &code) == errSecSuccess, let code else { throw NativeAuthFailure.invalidConfiguration }
    var staticCode: SecStaticCode?
    guard SecCodeCopyStaticCode(code, [], &staticCode) == errSecSuccess, let staticCode else {
      throw NativeAuthFailure.invalidConfiguration
    }
    var result: CFDictionary?
    guard
      SecCodeCopySigningInformation(staticCode, SecCSFlags(rawValue: kSecCSSigningInformation), &result)
        == errSecSuccess,
      let info = result as? [String: Any]
    else { throw NativeAuthFailure.invalidConfiguration }
    return try scope(
      team: info[kSecCodeInfoTeamIdentifier as String] as? String,
      uniqueCode: info[kSecCodeInfoUnique as String] as? Data)
  }
}
