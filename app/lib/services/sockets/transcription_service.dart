import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/message_event.dart';
import 'package:omi/backend/schema/transcript_segment.dart';
import 'package:omi/env/env.dart';
import 'package:omi/models/custom_stt_config.dart';
import 'package:omi/models/stt_provider.dart';
import 'package:omi/env/environment_profile.dart';
import 'package:omi/services/sockets/on_device_apple_provider.dart';
import 'package:omi/services/sockets/on_device_whisper_provider.dart';
import 'package:omi/services/sockets/pure_socket.dart';
import 'package:omi/services/sockets/transcription_service.dart';
import 'package:omi/utils/debug_log_manager.dart';
import 'package:omi/utils/hard_secret_detector.dart';
import 'package:omi/utils/logger.dart';

export 'package:omi/utils/audio/audio_transcoder.dart';
export 'package:omi/services/sockets/composite_transcription_socket.dart';
export 'package:omi/services/sockets/pure_polling.dart';
export 'package:omi/services/sockets/pure_streaming_stt.dart';
export 'package:omi/models/stt_response_schema.dart';
export 'package:omi/models/stt_result.dart';
export 'package:omi/services/sockets/transcription_polling_service.dart';

abstract interface class ITransctiptSegmentSocketServiceListener {
  void onMessageEventReceived(MessageEvent event);

  void onSegmentReceived(List<TranscriptSegment> segments);

  void onError(Object err);

  void onConnected();

  void onClosed([int? closeCode]);
}

class SpeechProfileTranscriptSegmentSocketService extends TranscriptSegmentSocketService {
  SpeechProfileTranscriptSegmentSocketService.create(
    super.sampleRate,
    super.codec,
    super.language, {
    super.source,
    super.customSttMode,
    super.onboardingMode,
  }) : super.create(includeSpeechProfile: false);
}

class ConversationTranscriptSegmentSocketService extends TranscriptSegmentSocketService {
  ConversationTranscriptSegmentSocketService.create(
    super.sampleRate,
    super.codec,
    super.language, {
    super.source,
    super.customSttMode,
  }) : super.create(includeSpeechProfile: true);
}

class CustomSttTranscriptSegmentSocketService extends TranscriptSegmentSocketService {
  CustomSttTranscriptSegmentSocketService.create(super.sampleRate, super.codec, super.language, {super.source})
      : super.create(includeSpeechProfile: true, customSttMode: true);
}

enum SocketServiceState { connected, disconnected }

class TranscriptSegmentSocketService implements IPureSocketListener {
  late IPureSocket _socket;
  final Map<Object, ITransctiptSegmentSocketServiceListener> _listeners = {};

  /// Access to the underlying socket (for composite service creation)
  IPureSocket get socket => _socket;

  SocketServiceState get state =>
      _socket.status == PureSocketStatus.connected ? SocketServiceState.connected : SocketServiceState.disconnected;

  int sampleRate;
  BleAudioCodec codec;
  String language;
  bool includeSpeechProfile;
  String? source;
  bool customSttMode;
  String? sttConfigId;

  bool onboardingMode;

  TranscriptSegmentSocketService.create(
    this.sampleRate,
    this.codec,
    this.language, {
    this.includeSpeechProfile = false,
    this.source,
    this.customSttMode = false,
    this.sttConfigId,
    this.onboardingMode = false,
  }) {
    var params = '?language=$language&sample_rate=$sampleRate&codec=$codec&uid=${SharedPreferencesUtil().uid}'
        '&include_speech_profile=$includeSpeechProfile&stt_service=${SharedPreferencesUtil().transcriptionModel}'
        '&conversation_timeout=${SharedPreferencesUtil().conversationSilenceDuration}';

    if (source != null && source!.isNotEmpty) {
      params += '&source=${Uri.encodeComponent(source!)}';
    }

    if (customSttMode) {
      params += '&custom_stt=enabled';
    }

    if (onboardingMode) {
      params += '&onboarding=enabled';
    }

    // Enable server-side speaker auto-assignment (backward compatibility flag)
    params += '&speaker_auto_assign=enabled';

    // Whether the backend may auto-create a new person when it detects a name.
    // Mirrors the user's "Auto-create Speakers" setting; a detected name with no
    // existing match is still surfaced for manual tagging when this is off.
    params += '&create_speakers=${SharedPreferencesUtil().autoCreateSpeakersEnabled}';

    if (SharedPreferencesUtil().vadGateEnabled) {
      params += '&vad_gate=enabled';
    }

    String url =
        Env.apiBaseUrl!.replaceFirst('https://', 'wss://').replaceFirst('http://', 'ws://') + 'v4/listen$params';

    _socket = PureSocket(url);
    _socket.setListener(this);
  }

  TranscriptSegmentSocketService.withSocket(
    this.sampleRate,
    this.codec,
    this.language,
    IPureSocket socket, {
    this.includeSpeechProfile = false,
    this.source,
    this.customSttMode = false,
    this.sttConfigId,
    this.onboardingMode = false,
  }) {
    _socket = socket;
    _socket.setListener(this);
  }

  void subscribe(Object context, ITransctiptSegmentSocketServiceListener listener) {
    _listeners.remove(context.hashCode);
    _listeners.putIfAbsent(context.hashCode, () => listener);
  }

  void unsubscribe(Object context) {
    _listeners.remove(context.hashCode);
  }

  Future start() async {
    bool ok = await _socket.connect();
    if (!ok) {
      Logger.debug("Can not connect to websocket");
      await DebugLogManager.logWarning('transcription_socket_connect_failed', {
        'url': Env.apiBaseUrl?.replaceAll('https', 'wss') ?? 'null',
        'sample_rate': sampleRate,
        'codec': codec.toString(),
        'language': language,
      });
    }
  }

  Future stop({String? reason}) async {
    await _socket.stop();
    _listeners.clear();

    if (reason != null) {
      Logger.debug(reason);
      await DebugLogManager.logInfo('transcription_socket_stopped', {'reason': reason});
    }
  }

  Future send(dynamic message) async {
    _socket.send(message);
    return;
  }

  Future sendText(String message) async {
    _socket.send(message);
    return;
  }

  Future requestFirstOnboardingQuestion() async {
    await sendText(jsonEncode({'type': 'start_onboarding'}));
  }

  @override
  void onClosed([int? closeCode]) {
    _listeners.forEach((k, v) {
      v.onClosed(closeCode);
    });
    DebugLogManager.logEvent('transcription_socket_closed', {'close_code': closeCode ?? -1});
  }

  @override
  void onError(Object err, StackTrace trace) {
    _listeners.forEach((k, v) {
      v.onError(err);
    });
    DebugLogManager.logError(err, trace, 'transcription_socket_error');
  }

  @override
  void onMessage(event) {
    // Decode json
    dynamic jsonEvent;
    try {
      jsonEvent = jsonDecode(event);
    } on FormatException catch (e) {
      Logger.debug(e.toString());
      DebugLogManager.logWarning('transcription_socket_parse_error', {'error': e.toString()});
    }
    if (jsonEvent == null) {
      Logger.debug("Can not decode message event json $event");
      return;
    }

    // Transcript segments
    if (jsonEvent is List) {
      var segments = _dropSecretSegments(jsonEvent);
      if (segments.isEmpty) {
        return;
      }
      _listeners.forEach((k, v) {
        v.onSegmentReceived(segments.map((e) => TranscriptSegment.fromJson(e)).toList());
      });
      return;
    }

    // Message event
    if (jsonEvent.containsKey("type")) {
      var event = MessageEvent.fromJson(jsonEvent);
      _listeners.forEach((k, v) {
        v.onMessageEventReceived(event);
      });
      return;
    }

    Logger.debug(event.toString());
    DebugLogManager.logInfo('transcription_socket_unhandled_message: ${event.toString()}');
  }

  List<dynamic> _dropSecretSegments(List<dynamic> segments) {
    final kept = <dynamic>[];
    final categories = <String>{};
    var droppedCount = 0;
    for (final segment in segments) {
      final text = segment is Map ? segment['text']?.toString() : null;
      if (text != null && HardSecretDetector.contains(text)) {
        categories.addAll(HardSecretDetector.categories(text));
        droppedCount += 1;
        continue;
      }
      kept.add(segment);
    }
    if (droppedCount > 0) {
      final sortedCategories = categories.toList()..sort();
      unawaited(
        DebugLogManager.logEvent('hard_secret_artifact_dropped', {
          'source': 'transcription_socket',
          'artifact_type': 'transcript_segment',
          'dropped_count': droppedCount,
          'categories': sortedCategories,
        }),
      );
    }
    return kept;
  }

  @override
  void onConnected() {
    _listeners.forEach((k, v) {
      v.onConnected();
    });
    DebugLogManager.logEvent('transcription_socket_connected', {
      'sample_rate': sampleRate,
      'codec': codec.toString(),
      'language': language,
      'include_speech_profile': includeSpeechProfile,
    });
  }
}

class TranscriptSocketServiceFactory {
  TranscriptSocketServiceFactory._();

  /// Codecs supported by custom STT providers
  static const List<BleAudioCodec> _customSttSupportedCodecs = [
    BleAudioCodec.pcm8,
    BleAudioCodec.pcm16,
    BleAudioCodec.opus,
    BleAudioCodec.opusFS320,
  ];

  /// Check if a codec is supported for custom STT
  static bool isCodecSupportedForCustomStt(BleAudioCodec codec) {
    return _customSttSupportedCodecs.contains(codec);
  }

  static bool shouldBlockUnsupportedCodecFallback(BleAudioCodec codec, CustomSttConfig config) {
    return config.isEnabled && !isCodecSupportedForCustomStt(codec) && !config.sendRawAudioToOmi;
  }

  /// Create default Omi transcription service
  static TranscriptSegmentSocketService createDefault(
    int sampleRate,
    BleAudioCodec codec,
    String language, {
    bool includeSpeechProfile = true,
    String? source,
    String? sttConfigId,
  }) {
    return TranscriptSegmentSocketService.create(
      sampleRate,
      codec,
      language,
      includeSpeechProfile: includeSpeechProfile,
      source: source,
      sttConfigId: sttConfigId ?? 'omi:default',
    );
  }

  /// Create speech profile transcription service
  static TranscriptSegmentSocketService createSpeechProfile(
    int sampleRate,
    BleAudioCodec codec,
    String language, {
    String? source,
  }) {
    return SpeechProfileTranscriptSegmentSocketService.create(sampleRate, codec, language, source: source);
  }

  /// Main entry point: Create transcription service from CustomSttConfig
  /// Uses config.isLive to decide between streaming and polling sockets
  static TranscriptSegmentSocketService createFromCustomConfig(
    int sampleRate,
    BleAudioCodec codec,
    String language,
    CustomSttConfig config, {
    String? source,
  }) {
    if (!config.isEnabled) {
      return createDefault(sampleRate, codec, language, source: source);
    }

    final sttConfigId = config.sttConfigId;
    final effectiveLang = config.effectiveLanguage;
    final effectiveModel = config.effectiveModel;
    Logger.debug(
      "[STTFactory] Creating socket: provider=${config.provider.name}, isLive=${config.isLive}, lang=$effectiveLang, model=$effectiveModel",
    );

    // Create primary socket based on isLive/isPolling
    final primarySocket = createTransportForProfile(
      profile: Env.profile,
      provider: config.provider,
      config: config,
      createTransport: () => config.isLive
          ? _createStreamingSocket(sampleRate, codec, config)
          : _createPollingSocket(sampleRate, codec, config),
    );

    // Wrap with composite service (primary STT + Omi backend)
    return _createCompositeService(
      sampleRate,
      codec,
      effectiveLang,
      primarySocket: primarySocket,
      source: source,
      sttConfigId: sttConfigId,
      sttProvider: config.provider.name,
      forwardRawAudioToSecondary: config.sendRawAudioToOmi,
    );
  }

  static void validateDeploymentEgress({
    required AppEnvironmentProfile profile,
    required SttProvider provider,
    CustomSttConfig? config,
  }) {
    if (profile != AppEnvironmentProfile.selfHosted) return;

    if (!provider.isSelfHostedClientSafe) {
      throw StateError(
        'Profile self_hosted routes network transcription through its configured backend; '
        'client-direct ${provider.name} is unavailable.',
      );
    }

    // The provider enum alone is not an authority boundary: an imported or
    // persisted config can override request_type/url. Keep the two client-safe
    // providers on their intended local transports before any URL/HTTP/WS
    // object is constructed.
    if (provider == SttProvider.onDeviceWhisper &&
        config?.requestType != null &&
        config!.requestType != SttRequestType.multipartForm) {
      throw StateError('Profile self_hosted on-device transcription cannot use a network request type.');
    }

    if (provider == SttProvider.localWhisper) {
      if (config?.url?.trim().isNotEmpty == true) {
        throw StateError('Profile self_hosted local Whisper cannot override its local inference URL.');
      }
      if (config?.requestType != null && config!.requestType != SttRequestType.multipartForm) {
        throw StateError('Profile self_hosted local Whisper requires multipart_form transport.');
      }
      final host = config?.host?.trim() ?? '127.0.0.1';
      if (!_isPrivateLocalHost(host)) {
        throw StateError('Profile self_hosted local Whisper requires a loopback or private-network host.');
      }
    }
  }

  /// Compiler-visible network-object construction boundary. Production passes
  /// the socket/provider constructor as [createTransport]; self-hosted rejection
  /// happens before that closure can read a URL or instantiate an HTTP/WS client.
  static T createTransportForProfile<T>({
    required AppEnvironmentProfile profile,
    required SttProvider provider,
    CustomSttConfig? config,
    required T Function() createTransport,
  }) {
    validateDeploymentEgress(profile: profile, provider: provider, config: config);
    return createTransport();
  }

  /// A self-hosted mobile client may use a Whisper process on the device or a
  /// private operator network, but must not turn the "local" provider into a
  /// public vendor egress path. Public operator STT belongs behind the
  /// configured backend provider route and is therefore not accepted here.
  static bool _isPrivateLocalHost(String rawHost) {
    var host = rawHost.trim().toLowerCase();
    if (host.isEmpty || host.contains(RegExp(r'[/:?#@\s]'))) return false;
    host = host.replaceFirst(RegExp(r'\.+$'), '');
    if (host == 'localhost' || host == 'host.docker.internal' || host.endsWith('.local')) return true;

    final address = InternetAddress.tryParse(host);
    if (address == null) return false;
    if (address.type == InternetAddressType.IPv4) {
      final octets = host.split('.').map(int.tryParse).toList();
      if (octets.length != 4 || octets.any((octet) => octet == null || octet < 0 || octet > 255)) return false;
      final first = octets[0]!;
      final second = octets[1]!;
      return first == 10 ||
          (first == 172 && second >= 16 && second <= 31) ||
          (first == 192 && second == 168) ||
          first == 127 ||
          (first == 100 && second >= 64 && second <= 127);
    }

    // IPv6 loopback, link-local, and unique-local addresses are private to
    // the device/operator network. InternetAddress normalizes neither textual
    // form here, so use the canonical address string for the prefix check.
    final normalized = address.address.toLowerCase();
    return address.isLoopback ||
        normalized.startsWith('fe8') ||
        normalized.startsWith('fe9') ||
        normalized.startsWith('fea') ||
        normalized.startsWith('feb') ||
        normalized.startsWith('fc') ||
        normalized.startsWith('fd');
  }

  /// Create streaming WebSocket for live STT
  static IPureSocket _createStreamingSocket(int sampleRate, BleAudioCodec codec, CustomSttConfig config) {
    final transcoder = AudioTranscoderFactory.createToRawPcm(sourceCodec: codec, sampleRate: sampleRate);

    // Special case: Gemini Live has unique protocol (setup message, base64 audio)
    if (config.provider == SttProvider.geminiLive) {
      return GeminiStreamingSttSocket(
        apiKey: config.apiKey ?? '',
        model:
            config.effectiveModel.isNotEmpty ? config.effectiveModel : 'gemini-2.5-flash-native-audio-preview-12-2025',
        language: config.effectiveLanguage,
        sampleRate: sampleRate,
        transcoder: transcoder,
      );
    }

    // Deepgram Live and other streaming providers
    final requestConfig = config.requestConfig;
    final url = requestConfig['url'] ?? config.effectiveUrl;
    final headers =
        requestConfig['headers'] != null ? Map<String, String>.from(requestConfig['headers']) : (config.headers ?? {});
    final params =
        requestConfig['params'] != null ? Map<String, String>.from(requestConfig['params']) : (config.params ?? {});

    // Build WebSocket URL with query params
    final wsUrl = _buildUrlWithParams(url, params);

    return PureStreamingSttSocket(
      config: StreamingSttConfig.schemaBased(
        wsUrl: wsUrl,
        schema: config.schema,
        headers: headers,
        transcoder: transcoder,
        serviceId: config.provider.name,
        sendKeepAlive: config.provider == SttProvider.deepgramLive,
        keepAliveInterval: const Duration(seconds: 8),
      ),
    );
  }

  /// Create polling HTTP socket for batch STT
  static IPureSocket _createPollingSocket(int sampleRate, BleAudioCodec codec, CustomSttConfig config) {
    final transcoder = AudioTranscoderFactory.createToWav(sourceCodec: codec, sampleRate: sampleRate);

    final requestConfig = config.requestConfig;
    final url = requestConfig['url'] ?? config.effectiveUrl;
    final headers =
        requestConfig['headers'] != null ? Map<String, String>.from(requestConfig['headers']) : (config.headers ?? {});
    final params =
        requestConfig['params'] != null ? Map<String, String>.from(requestConfig['params']) : (config.params ?? {});
    final audioFieldName = requestConfig['audio_field_name'] ?? config.audioFieldName ?? 'file';
    final requestType = config.effectiveRequestType;

    // Build URL with query params for raw_binary type
    final effectiveUrl = requestType == SttRequestType.rawBinary ? _buildUrlWithParams(url, params) : url;

    // Special handling for On-Device Whisper
    if (config.provider == SttProvider.onDeviceWhisper) {
      // Use Native iOS Speech Recognition on iOS
      if (Platform.isIOS) {
        return PurePollingSocket(
          config: AudioPollingConfig(
            bufferDuration: const Duration(seconds: 5),
            minBufferSizeBytes: sampleRate * 2,
            serviceId: config.provider.name,
            transcoder: transcoder,
          ),
          sttProvider: OnDeviceAppleProvider(language: config.language ?? 'en'),
        );
      }

      if (config.url == null || config.url!.isEmpty) {
        throw ArgumentError("[STTFactory] OnDeviceWhisper selected but no model path provided.");
      }
      return PurePollingSocket(
        config: AudioPollingConfig(
          bufferDuration: const Duration(seconds: 5),
          minBufferSizeBytes: sampleRate * 2,
          serviceId: config.provider.name,
          transcoder: transcoder,
        ),
        sttProvider: OnDeviceWhisperProvider(modelPath: config.url ?? '', language: config.language ?? 'en'),
      );
    }

    return PurePollingSocket(
      config: AudioPollingConfig(
        bufferDuration: const Duration(seconds: 5),
        minBufferSizeBytes: sampleRate * 2,
        serviceId: config.provider.name,
        transcoder: transcoder,
      ),
      sttProvider: SchemaBasedSttProvider(
        apiUrl: effectiveUrl,
        schema: config.schema,
        defaultHeaders: headers,
        defaultFields: requestType == SttRequestType.rawBinary ? {} : params,
        audioFieldName: audioFieldName,
        requestType: requestType,
      ),
    );
  }

  /// Build URL with query parameters
  static String _buildUrlWithParams(String baseUrl, Map<String, String> params) {
    if (params.isEmpty) return baseUrl;
    final uri = Uri.tryParse(baseUrl);
    if (uri == null) {
      Logger.warning('[STTFactory] Invalid URL, cannot append params: $baseUrl');
      return baseUrl;
    }
    return uri.replace(queryParameters: {...uri.queryParameters, ...params}).toString();
  }

  /// Create composite service: primary STT socket + Omi backend for conversation processing
  static TranscriptSegmentSocketService _createCompositeService(
    int sampleRate,
    BleAudioCodec codec,
    String language, {
    required IPureSocket primarySocket,
    String? source,
    String? sttConfigId,
    String? sttProvider,
    required bool forwardRawAudioToSecondary,
  }) {
    final secondaryService = CustomSttTranscriptSegmentSocketService.create(
      sampleRate,
      codec,
      language,
      source: source,
    );
    final compositeSocket = CompositeTranscriptionSocket(
      primarySocket: primarySocket,
      secondarySocket: secondaryService.socket,
      sttProvider: sttProvider,
      forwardRawAudioToSecondary: forwardRawAudioToSecondary,
    );
    return TranscriptSegmentSocketService.withSocket(
      sampleRate,
      codec,
      language,
      compositeSocket,
      source: source,
      customSttMode: true,
      sttConfigId: sttConfigId,
    );
  }
}
