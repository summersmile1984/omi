import 'package:flutter/foundation.dart';
import 'package:package_info_plus/package_info_plus.dart';

import 'client.dart';
import 'credential.dart';
import 'deployment.dart';
import 'owner.dart';
import 'secure_store.dart';

final class NativeIdentity {
  static final owner = IdentityOwner(
      client: IdentityClient(deployment: NativeDeployment.current),
      store: SecureOpaqueCredentialStore(NativeDeployment.current));
  static IdentityFailure? restoreFailure;

  static Future<void> initialize() async {
    final package = await PackageInfo.fromPlatform();
    if (kReleaseMode || package.packageName != NativeDeployment.current.packageId) {
      throw StateError('This local identity artifact does not match its package contract');
    }
    await restore();
  }

  static Future<void> restore() async {
    restoreFailure = null;
    try {
      await owner.restore();
    } on IdentityException catch (error) {
      restoreFailure = error.failure;
    }
  }
}
