import 'dart:io';

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:font_awesome_flutter/font_awesome_flutter.dart';
import 'package:provider/provider.dart';

import 'package:omi/providers/auth_provider.dart';
import 'package:omi/utils/l10n_extensions.dart';

class AuthComponent extends StatefulWidget {
  final VoidCallback onSignIn;

  const AuthComponent({super.key, required this.onSignIn});

  @override
  State<AuthComponent> createState() => _AuthComponentState();
}

class _AuthComponentState extends State<AuthComponent> {
  Future<void> _showBetterAuthDialog(AuthenticationProvider provider) async {
    final emailController = TextEditingController();
    final passwordController = TextEditingController();
    final nameController = TextEditingController();
    var createAccount = false;
    var submitting = false;
    String? formError;
    try {
      await showDialog<void>(
        context: context,
        builder: (dialogContext) => StatefulBuilder(
          builder: (dialogContext, setDialogState) => AlertDialog(
            backgroundColor: const Color(0xFF111111),
            title: Text(
              createAccount ? dialogContext.l10n.createAccountTitle : dialogContext.l10n.signInTitle,
              style: const TextStyle(color: Colors.white),
            ),
            content: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (createAccount)
                  TextField(
                    controller: nameController,
                    textInputAction: TextInputAction.next,
                    style: const TextStyle(color: Colors.white),
                    decoration: InputDecoration(labelText: dialogContext.l10n.nameLabel),
                  ),
                TextField(
                  key: const Key('betterAuthEmailField'),
                  controller: emailController,
                  keyboardType: TextInputType.emailAddress,
                  textInputAction: TextInputAction.next,
                  autocorrect: false,
                  style: const TextStyle(color: Colors.white),
                  decoration: InputDecoration(labelText: dialogContext.l10n.emailLabel),
                ),
                TextField(
                  key: const Key('betterAuthPasswordField'),
                  controller: passwordController,
                  obscureText: true,
                  enableSuggestions: false,
                  autocorrect: false,
                  style: const TextStyle(color: Colors.white),
                  decoration: InputDecoration(labelText: dialogContext.l10n.passwordLabel),
                ),
                if (formError != null)
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: Text(formError!, style: const TextStyle(color: Colors.redAccent)),
                  ),
                TextButton(
                  onPressed: submitting
                      ? null
                      : () => setDialogState(() {
                            createAccount = !createAccount;
                            formError = null;
                          }),
                  child: Text(
                    createAccount ? dialogContext.l10n.alreadyHaveAccountLogin : dialogContext.l10n.createAccountTitle,
                  ),
                ),
              ],
            ),
            actions: [
              TextButton(
                onPressed: submitting ? null : () => Navigator.of(dialogContext).pop(),
                child: Text(dialogContext.l10n.cancel),
              ),
              FilledButton(
                key: const Key('betterAuthEmailSubmit'),
                onPressed: submitting
                    ? null
                    : () async {
                        final email = emailController.text.trim();
                        String? validationError;
                        if (email.isEmpty) {
                          validationError = dialogContext.l10n.enterEmailError;
                        } else if (!email.contains('@')) {
                          validationError = dialogContext.l10n.invalidEmailError;
                        } else if (passwordController.text.isEmpty) {
                          validationError = dialogContext.l10n.enterPasswordError;
                        } else if (passwordController.text.length < 8) {
                          validationError = dialogContext.l10n.passwordMinLengthError;
                        } else if (createAccount && nameController.text.trim().isEmpty) {
                          validationError = dialogContext.l10n.enterNameError;
                        }
                        if (validationError != null) {
                          setDialogState(() => formError = validationError);
                          return;
                        }
                        setDialogState(() {
                          submitting = true;
                          formError = null;
                        });
                        final signedIn = await provider.onBetterAuthEmailSignIn(
                          email: email,
                          password: passwordController.text,
                          name: nameController.text,
                          createAccount: createAccount,
                        );
                        if (!dialogContext.mounted) return;
                        if (signedIn) {
                          Navigator.of(dialogContext).pop();
                          widget.onSignIn();
                        } else {
                          setDialogState(() => submitting = false);
                        }
                      },
                child: submitting
                    ? const SizedBox.square(dimension: 18, child: CircularProgressIndicator(strokeWidth: 2))
                    : Text(createAccount ? dialogContext.l10n.signUpButton : dialogContext.l10n.signInButton),
              ),
            ],
          ),
        ),
      );
    } finally {
      emailController.dispose();
      passwordController.dispose();
      nameController.dispose();
    }
  }

  @override
  Widget build(BuildContext context) {
    return Consumer<AuthenticationProvider>(
      builder: (context, provider, child) {
        return Column(
          children: [
            // Background image area - takes remaining space
            Expanded(
              child: Container(), // Just takes up space for background image
            ),

            // Bottom drawer card - wraps content
            Container(
              width: double.infinity,
              padding: EdgeInsets.fromLTRB(32, 26, 32, MediaQuery.of(context).padding.bottom + 8),
              decoration: const BoxDecoration(
                color: Colors.black,
                borderRadius: BorderRadius.only(topLeft: Radius.circular(40), topRight: Radius.circular(40)),
              ),
              child: SafeArea(
                top: false,
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    // Loading indicator or spacing
                    SizedBox(
                      height: 20,
                      child: provider.loading
                          ? const Center(
                              child: CircularProgressIndicator(valueColor: AlwaysStoppedAnimation(Colors.white)),
                            )
                          : null,
                    ),

                    // Title text
                    Text(
                      context.l10n.speakTranscribeSummarize,
                      style: const TextStyle(
                        color: Colors.white,
                        fontSize: 32,
                        fontWeight: FontWeight.bold,
                        height: 1.2,
                        fontFamily: 'Manrope',
                      ),
                      textAlign: TextAlign.center,
                    ),

                    const SizedBox(height: 32),

                    // Sign in buttons
                    if (!provider.betterAuthEnabled && (Platform.isIOS || Platform.isAndroid)) ...[
                      SizedBox(
                        width: double.infinity,
                        height: 56,
                        child: ElevatedButton(
                          onPressed: () {
                            HapticFeedback.mediumImpact();
                            provider.onAppleSignIn(widget.onSignIn);
                          },
                          style: ElevatedButton.styleFrom(
                            backgroundColor: Colors.white,
                            foregroundColor: Colors.black,
                            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(28)),
                          ),
                          child: Row(
                            mainAxisAlignment: MainAxisAlignment.center,
                            children: [
                              const FaIcon(FontAwesomeIcons.apple, size: 24),
                              const SizedBox(width: 8),
                              Text(
                                context.l10n.signInWithApple,
                                style: const TextStyle(
                                  fontSize: 18,
                                  fontWeight: FontWeight.w600,
                                  fontFamily: 'Manrope',
                                ),
                              ),
                            ],
                          ),
                        ),
                      ),
                      const SizedBox(height: 16),
                    ],

                    if (provider.betterAuthEnabled) ...[
                      // Better Auth is available only when the build explicitly
                      // selects the operator identity provider.
                      SizedBox(
                        width: double.infinity,
                        height: 56,
                        child: ElevatedButton(
                          key: const Key('betterAuthSignInButton'),
                          onPressed: () {
                            HapticFeedback.mediumImpact();
                            _showBetterAuthDialog(provider);
                          },
                          style: ElevatedButton.styleFrom(
                            backgroundColor: Colors.black,
                            foregroundColor: Colors.white,
                            side: const BorderSide(color: Colors.white24),
                            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(28)),
                          ),
                          child: const Row(
                            mainAxisAlignment: MainAxisAlignment.center,
                            children: [
                              FaIcon(FontAwesomeIcons.server, size: 20),
                              SizedBox(width: 8),
                              Text(
                                'Better Auth (self-hosted)',
                                style: TextStyle(fontSize: 18, fontWeight: FontWeight.w600, fontFamily: 'Manrope'),
                              ),
                            ],
                          ),
                        ),
                      ),
                      const SizedBox(height: 16),
                    ],

                    if (!provider.betterAuthEnabled)
                      // Google sign in button
                      SizedBox(
                        width: double.infinity,
                        height: 56,
                        child: ElevatedButton(
                          onPressed: () {
                            HapticFeedback.mediumImpact();
                            provider.onGoogleSignIn(widget.onSignIn);
                          },
                          style: ElevatedButton.styleFrom(
                            backgroundColor: Colors.white,
                            foregroundColor: Colors.black,
                            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(28)),
                          ),
                          child: Row(
                            mainAxisAlignment: MainAxisAlignment.center,
                            children: [
                              const FaIcon(FontAwesomeIcons.google, size: 20),
                              const SizedBox(width: 8),
                              Text(
                                context.l10n.signInWithGoogle,
                                style: const TextStyle(
                                  fontSize: 18,
                                  fontWeight: FontWeight.w600,
                                  fontFamily: 'Manrope',
                                ),
                              ),
                            ],
                          ),
                        ),
                      ),

                    const SizedBox(height: 24),

                    // Privacy policy text (same as welcome page)
                    RichText(
                      textAlign: TextAlign.center,
                      text: TextSpan(
                        style: TextStyle(
                          color: Colors.white.withValues(alpha: 0.6),
                          fontSize: 11,
                          fontFamily: 'Manrope',
                        ),
                        children: [
                          TextSpan(text: context.l10n.byContinuingAgree),
                          TextSpan(
                            text: context.l10n.privacyPolicy,
                            style: const TextStyle(decoration: TextDecoration.underline),
                            recognizer: TapGestureRecognizer()..onTap = provider.openPrivacyPolicy,
                          ),
                          const TextSpan(text: ' & '),
                          TextSpan(
                            text: context.l10n.termsOfUse,
                            style: const TextStyle(decoration: TextDecoration.underline),
                            recognizer: TapGestureRecognizer()..onTap = provider.openTermsOfService,
                          ),
                          const TextSpan(text: '.'),
                        ],
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ],
        );
      },
    );
  }
}
