import 'package:flutter_test/flutter_test.dart';
import 'package:omi/env/environment_profile.dart';
import 'package:omi/models/custom_stt_config.dart';
import 'package:omi/models/stt_provider.dart';
import 'package:omi/services/sockets/transcription_service.dart';

void main() {
  test('self-hosted rejects client-direct vendor STT before transport construction', () {
    for (final provider in [
      SttProvider.openai,
      SttProvider.deepgramLive,
      SttProvider.geminiLive,
      SttProvider.customLive,
    ]) {
      var constructed = false;
      expect(
        () => TranscriptSocketServiceFactory.createTransportForProfile<Object>(
          profile: AppEnvironmentProfile.selfHosted,
          provider: provider,
          config: CustomSttConfig(provider: provider),
          createTransport: () {
            constructed = true;
            return Object();
          },
        ),
        throwsStateError,
        reason: provider.name,
      );
      expect(constructed, isFalse, reason: provider.name);
    }
  });

  test('self-hosted rejects persisted local-provider URL and transport overrides', () {
    final bypassConfigs = [
      const CustomSttConfig(provider: SttProvider.localWhisper, url: 'wss://api.deepgram.com/v1/listen'),
      const CustomSttConfig(provider: SttProvider.localWhisper, host: 'generativelanguage.googleapis.com'),
      const CustomSttConfig(
        provider: SttProvider.onDeviceWhisper,
        requestType: SttRequestType.streaming,
        url: 'wss://api.openai.com/v1/audio/transcriptions',
      ),
    ];

    for (final config in bypassConfigs) {
      var constructed = false;
      expect(
        () => TranscriptSocketServiceFactory.createTransportForProfile<Object>(
          profile: AppEnvironmentProfile.selfHosted,
          provider: config.provider,
          config: config,
          createTransport: () {
            constructed = true;
            return Object();
          },
        ),
        throwsStateError,
        reason: config.provider.name,
      );
      expect(constructed, isFalse, reason: config.provider.name);
    }
  });

  test('self-hosted permits a private local Whisper endpoint', () {
    expect(
      () => TranscriptSocketServiceFactory.validateDeploymentEgress(
        profile: AppEnvironmentProfile.selfHosted,
        provider: SttProvider.localWhisper,
        config: const CustomSttConfig(provider: SttProvider.localWhisper, host: '192.168.20.15', port: 8080),
      ),
      returnsNormally,
    );
  });

  test('managed profiles keep the existing provider catalog', () {
    expect(SttProvider.openai.isSelfHostedClientSafe, isFalse);
    expect(SttProvider.localWhisper.isSelfHostedClientSafe, isTrue);
    expect(
      () => TranscriptSocketServiceFactory.validateDeploymentEgress(
        profile: AppEnvironmentProfile.production,
        provider: SttProvider.openai,
      ),
      returnsNormally,
    );
  });
}
