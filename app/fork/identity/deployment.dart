import 'dart:convert';
import 'package:crypto/crypto.dart';

/// Resolved build input. It contains public topology and package identity only.
final class NativeDeployment {
  NativeDeployment._(
      {required this.name,
      required this.target,
      required this.stage,
      required this.packageId,
      required this.productName,
      required this.api,
      required this.auth,
      required this.web,
      required this.mcp,
      required this.share,
      required this.objects,
      required this.terms,
      required this.privacy});

  final String name, target, stage, packageId, productName;
  final Uri api, auth, web, mcp, share, objects, terms, privacy;
  String get keychainPrefix => '$packageId.${sha256.convert(utf8.encode('$name|$auth'))}';
  static final current = NativeDeployment.decode(const String.fromEnvironment('OMI_FORK_DEPLOYMENT_JSON'));

  factory NativeDeployment.decode(String encoded) {
    try {
      final map = jsonDecode(encoded) as Map<String, dynamic>;
      final row = map['profile'] as Map<String, dynamic>;
      final target = row['target'] as String, stage = row['stage'] as String;
      if (!{'self_hosted', 'cloudflare'}.contains(target) ||
          stage != 'local' ||
          row['name'] != '$target.$stage' ||
          row['identity_provider'] != 'better_auth') {
        throw const FormatException('Invalid deployment identity');
      }
      Uri endpoint(String key) {
        final url = Uri.parse(row[key] as String);
        if (!url.hasAuthority ||
            url.host.isEmpty ||
            !{'http', 'https'}.contains(url.scheme) ||
            url.userInfo.isNotEmpty ||
            url.hasQuery ||
            url.hasFragment) {
          throw const FormatException('Invalid endpoint');
        }
        return url;
      }

      String identity(String key) {
        final value = map[key];
        if (value is! String || value.isEmpty) throw const FormatException('Missing identity');
        return value;
      }

      final auth = endpoint('auth_base_url');
      if (auth.path.isNotEmpty && auth.path != '/') throw const FormatException('Auth must be an origin');
      return NativeDeployment._(
          name: row['name'] as String,
          target: target,
          stage: stage,
          packageId: identity('package_id'),
          productName: identity('product_name'),
          api: endpoint('api_base_url'),
          auth: auth,
          web: endpoint('web_base_url'),
          mcp: endpoint('mcp_base_url'),
          share: endpoint('share_base_url'),
          objects: endpoint('objects_base_url'),
          terms: Uri.parse(identity('terms')),
          privacy: Uri.parse(identity('privacy')));
    } catch (_) {
      throw const FormatException('Invalid native deployment configuration');
    }
  }

  Uri authEndpoint(String path) => auth.replace(path: '/api/auth/$path');
  String get apiBase => '${api.toString().replaceFirst(RegExp(r'/+$'), '')}/';
}
