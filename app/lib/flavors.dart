import 'package:flutter/services.dart';

import 'package:omi/utils/logger.dart';

enum Environment {
  prod,
  dev;

  static Environment fromFlavor() {
    return fromFlavorName(appFlavor);
  }

  static Environment fromFlavorName(String? flavor) {
    switch (flavor?.trim().toLowerCase()) {
      case 'prod':
      case 'selfhost':
        return Environment.prod;
      case 'dev':
        return Environment.dev;
      default:
        Logger.debug('Warning: Unknown flavor "$flavor", defaulting to dev');
        return Environment.dev;
    }
  }
}

class F {
  static Environment env = Environment.fromFlavor();

  static String get title {
    switch (env) {
      case Environment.prod:
        return 'Omi';
      case Environment.dev:
        return 'Omi Dev';
    }
  }
}
