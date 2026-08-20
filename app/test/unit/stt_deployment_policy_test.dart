import 'package:flutter_test/flutter_test.dart';
import 'package:omi/env/environment_profile.dart';
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
