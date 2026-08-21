import 'package:flutter_test/flutter_test.dart';
import 'package:omi/env/environment_profile.dart';
import 'package:omi/models/custom_stt_config.dart';
import 'package:omi/models/stt_provider.dart';
import 'package:omi/services/sockets/transcription_service.dart';

void main() {
  test('self-hosted profile rejects every client-direct network STT provider before transport', () {
    for (final provider in [
      SttProvider.openai,
      SttProvider.openaiDiarize,
      SttProvider.deepgram,
      SttProvider.deepgramLive,
      SttProvider.falai,
      SttProvider.gemini,
      SttProvider.geminiLive,
      SttProvider.omiParakeet,
      SttProvider.custom,
      SttProvider.customLive,
    ]) {
      expect(
        () => TranscriptSocketServiceFactory.validateDeploymentEgress(
          profile: AppEnvironmentProfile.selfHosted,
          provider: provider,
        ),
        throwsStateError,
        reason: provider.name,
      );
    }
  });

  test('self-hosted profile retains backend and local transcription paths', () {
    for (final provider in [
      SttProvider.omi,
      SttProvider.localWhisper,
      SttProvider.onDeviceWhisper,
    ]) {
      expect(
        () => TranscriptSocketServiceFactory.validateDeploymentEgress(
          profile: AppEnvironmentProfile.selfHosted,
          provider: provider,
        ),
        returnsNormally,
        reason: provider.name,
      );
    }
  });

  test('self-hosted rejection occurs before a transport object is constructed', () {
    var constructed = false;
    expect(
      () => TranscriptSocketServiceFactory.createTransportForProfile<Object>(
        profile: AppEnvironmentProfile.selfHosted,
        provider: SttProvider.omiParakeet,
        createTransport: () {
          constructed = true;
          return Object();
        },
      ),
      throwsStateError,
    );
    expect(constructed, isFalse);
  });

  test('self-hosted rejects persisted local-provider URL and transport overrides before construction', () {
    final bypassConfigs = [
      const CustomSttConfig(
        provider: SttProvider.localWhisper,
        url: 'wss://api.deepgram.com/v1/listen',
      ),
      const CustomSttConfig(
        provider: SttProvider.localWhisper,
        host: 'generativelanguage.googleapis.com',
      ),
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

  test('self-hosted permits the intended private local Whisper endpoint', () {
    expect(
      () => TranscriptSocketServiceFactory.validateDeploymentEgress(
        profile: AppEnvironmentProfile.selfHosted,
        provider: SttProvider.localWhisper,
        config: const CustomSttConfig(
          provider: SttProvider.localWhisper,
          host: '192.168.20.15',
          port: 8080,
        ),
      ),
      returnsNormally,
    );
  });

  test('cloud profile keeps the existing BYOK provider catalog', () {
    expect(
      SttProviderConfig.providersForProfile(AppEnvironmentProfile.production).map((config) => config.provider),
      containsAll([SttProvider.openai, SttProvider.deepgramLive, SttProvider.geminiLive]),
    );
    expect(
      SttProviderConfig.providersForProfile(AppEnvironmentProfile.selfHosted).every(
        (config) => config.provider.isSelfHostedClientSafe,
      ),
      isTrue,
    );
  });
}
