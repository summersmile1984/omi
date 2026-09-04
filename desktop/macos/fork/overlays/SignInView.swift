import SwiftUI

struct SignInView: View {
  @ObservedObject var authState: AuthState
  @State private var email = ""
  @State private var password = ""
  @State private var name = ""
  @State private var createAccount = false

  var body: some View {
    VStack(alignment: .leading, spacing: 18) {
      Text(ForkDesktopBuild.productName).font(.largeTitle.weight(.semibold))
      Text(createAccount ? "Create an account" : "Sign in").font(.title2)
      if createAccount {
        TextField("Name", text: $name).accessibilityIdentifier("fork-auth-name")
      }
      TextField("Email", text: $email).accessibilityIdentifier("fork-auth-email")
      SecureField("Password", text: $password).accessibilityIdentifier("fork-auth-password")
      if let error = authState.error {
        Text(error).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
      }
      Button(createAccount ? "Create account" : "Sign in") {
        Task {
          do {
            try await AuthService.shared.signInWithEmail(
              email: email, password: password, name: createAccount ? name : nil
            )
          } catch {
            authState.error = error.localizedDescription
          }
        }
      }
      .keyboardShortcut(.defaultAction)
      .disabled(authState.isLoading || email.isEmpty || password.isEmpty || (createAccount && name.isEmpty))
      .accessibilityIdentifier("fork-auth-submit")
      Button(createAccount ? "Use an existing account" : "Create an account") {
        createAccount.toggle()
        authState.error = nil
      }
      .disabled(authState.isLoading)
      if authState.isLoading { ProgressView() }
      if authState.sessionPhase == .recoveryRequired {
        Button("Retry saved session") { Task { await AuthService.shared.retryRestoredSession() } }
      }
    }
    .textFieldStyle(.roundedBorder)
    .frame(maxWidth: 360)
    .padding(36)
  }
}
