import 'package:flutter/services.dart';

import 'package:omi/utils/logger.dart';
import 'flavors.brand.dart';

enum Environment {
  prod,
  dev;

  static Environment fromFlavor() {
    return Environment.values.firstWhere(
      (e) => e.name == appFlavor?.toLowerCase(),
      orElse: () {
        Logger.debug('Warning: Unknown flavor "$appFlavor", defaulting to dev');
        return Environment.dev;
      },
    );
  }
}

class F {
  static Environment env = Environment.fromFlavor();

  static String get title {
    switch (env) {
      case Environment.prod:
        return kBrandDisplayName;
      case Environment.dev:
        return '$kBrandDisplayName Dev';
    }
  }
}
