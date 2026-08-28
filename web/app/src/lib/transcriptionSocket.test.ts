import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./realtimeTicket', () => ({
  getRealtimeTicket: vi.fn(async () => 'short-lived-ticket'),
}));

vi.mock('./clientDevice', () => ({
  getWebDeviceIdHash: vi.fn(async () => 'device-1'),
}));

import { TranscriptionSocket } from './transcriptionSocket';

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static readonly OPEN = 1;
  readonly url: string;
  binaryType = '';
  readyState = FakeWebSocket.OPEN;
  sent: unknown[] = [];
  closeCode?: number;
  onopen?: () => void;
  onmessage?: (event: MessageEvent) => void;
  onerror?: (event: Event) => void;
  onclose?: (event: CloseEvent) => void;

  constructor(url: string | URL) {
    this.url = String(url);
    FakeWebSocket.instances.push(this);
  }

  send(data: unknown) {
    this.sent.push(data);
  }

  close(code = 1000, reason = '') {
    this.closeCode = code;
    this.onclose?.({ code, reason } as CloseEvent);
  }

  open() {
    this.onopen?.();
  }

  message(value: unknown) {
    this.onmessage?.({ data: JSON.stringify(value) } as MessageEvent);
  }
}

describe('TranscriptionSocket first-message authentication', () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeWebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('sends auth first and reports connected only after auth succeeds', async () => {
    const onConnected = vi.fn();
    const socket = new TranscriptionSocket({
      onSegment: vi.fn(),
      onError: vi.fn(),
      onConnected,
      onDisconnected: vi.fn(),
    });

    await socket.connect();
    const transport = FakeWebSocket.instances[0];
    transport.open();
    expect(JSON.parse(String(transport.sent[0]))).toEqual({
      type: 'auth',
      ticket: 'short-lived-ticket',
      device_id_hash: 'device-1',
    });
    expect(onConnected).not.toHaveBeenCalled();

    transport.message({ type: 'auth_response', success: true });
    expect(onConnected).toHaveBeenCalledOnce();
    socket.disconnect();
  });

  it('surfaces provider unavailability without reconnecting', async () => {
    const onError = vi.fn();
    const socket = new TranscriptionSocket({
      onSegment: vi.fn(),
      onError,
      onConnected: vi.fn(),
      onDisconnected: vi.fn(),
    });

    await socket.connect();
    const transport = FakeWebSocket.instances[0];
    transport.open();
    transport.message({
      type: 'auth_response',
      success: false,
      error: 'provider_unavailable',
    });

    expect(onError).toHaveBeenCalledWith('Transcription provider unavailable');
    expect(transport.closeCode).toBe(1013);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});
