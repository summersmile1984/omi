import { expect, test } from 'vitest';
import {
  buildPublicEnvironment,
  deriveWebSocketBaseUrl,
} from '../overlays/copy-moonshine-assets';

// The staged asset script is the only carrier left for the fork's brand
// environment: it decides which NEXT_PUBLIC_* keys survive into the client
// banner and the generated Moonshine server.
test('the branded asset script projects brand and profile keys into the public environment', () => {
  const environment = buildPublicEnvironment({
    NEXT_PUBLIC_API_BASE_URL: 'https://api.example.test/',
    NEXT_PUBLIC_BRAND_DISPLAY_NAME: 'Nimbus',
    NEXT_PUBLIC_BRAND_SHORT_NAME: 'Nimbus',
    NEXT_PUBLIC_BRAND_TAGLINE: 'think to ship',
    NEXT_PUBLIC_BRAND_APP_TITLE: 'Nimbus - think to ship',
    NEXT_PUBLIC_OMI_PRODUCT_NAME: 'Nimbus',
    NEXT_PUBLIC_OMI_PROFILE_JSON: '{"name":"fixture-dev"}',
  });
  expect(environment.NEXT_PUBLIC_BRAND_DISPLAY_NAME).toBe('Nimbus');
  expect(environment.NEXT_PUBLIC_BRAND_SHORT_NAME).toBe('Nimbus');
  expect(environment.NEXT_PUBLIC_BRAND_TAGLINE).toBe('think to ship');
  expect(environment.NEXT_PUBLIC_BRAND_APP_TITLE).toBe('Nimbus - think to ship');
  expect(environment.NEXT_PUBLIC_OMI_PRODUCT_NAME).toBe('Nimbus');
  expect(environment.NEXT_PUBLIC_OMI_PROFILE_JSON).toBe('{"name":"fixture-dev"}');
  expect(environment.NEXT_PUBLIC_WS_BASE_URL).toBe('wss://api.example.test');
});

test('the branded asset script keeps the upstream WebSocket derivation contract', () => {
  expect(deriveWebSocketBaseUrl('https://api.omiapi.com')).toBe('wss://api.omiapi.com');
  expect(deriveWebSocketBaseUrl('http://localhost:8000')).toBe('ws://localhost:8000');
  expect(
    buildPublicEnvironment({
      NEXT_PUBLIC_API_BASE_URL: 'https://api.omiapi.com',
      NEXT_PUBLIC_WS_BASE_URL: 'wss://recording.example.com',
    }).NEXT_PUBLIC_WS_BASE_URL,
  ).toBe('wss://recording.example.com');
});
