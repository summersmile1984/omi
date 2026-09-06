import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { Sidebar } from '@/components/layout/Sidebar';
import { GoalComposer } from '@/components/home/GoalComposer';
import { OmiOrb } from '@/components/ui/OmiOrb';
import { OmiPulseMark } from '@/components/ui/OmiPulseMark';
import { Footer } from '../overlays/Footer';
import HelpPage from '../overlays/HelpPage';
import { presentationFixture as brand } from './presentation-fixture';

vi.mock('canvas-confetti', () => ({ default: vi.fn() }));
vi.mock('@tschk/moonshine-next/navigation', () => ({ usePathname: () => '/home' }));
vi.mock('@tschk/moonshine-next/link', () => ({
  default: ({ href, children, ...props }: React.ComponentProps<'a'>) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));
vi.mock('@tschk/moonshine-next/image', () => ({
  default: ({ src, alt, className }: React.ComponentProps<'img'>) => (
    <img src={src} alt={alt} className={className} />
  ),
}));
vi.mock('@/components/auth/AuthProvider', () => ({
  useAuth: () => ({
    user: {
      displayName: 'Omi user notes',
      email: 'test@example.invalid',
      photoURL: null,
    },
    signOut: vi.fn(),
  }),
}));
vi.mock('@/components/notifications/NotificationContext', () => ({
  useNotificationContext: () => ({ toggleNotificationCenter: vi.fn(), unreadCount: 0 }),
}));
vi.mock('@/moonshine/register-client-route', () => ({ registerMoonshineRoute: vi.fn() }));

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem('sidebar-expanded', 'true');
  vi.stubEnv('NEXT_PUBLIC_OMI_PRODUCT_NAME', brand.product_name);
  vi.stubEnv('NEXT_PUBLIC_OMI_PRESENTATION_JSON', JSON.stringify(brand));
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 });
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: 900 });
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: () => ({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
    }),
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.clearAllMocks();
});

test('the transformed production sidebar uses the brand mark and download while retaining user text', async () => {
  render(<Sidebar isOpen onClose={vi.fn()} />);
  expect(await screen.findByRole('link', { name: 'Harbor' })).toHaveAttribute(
    'href',
    '/conversations',
  );
  expect(screen.getByRole('img', { name: 'Harbor' })).toHaveAttribute('src', '/logo.png');
  expect(screen.getByRole('link', { name: /Download Harbor for macOS/ })).toHaveAttribute(
    'href',
    brand.links.download,
  );
  fireEvent.click(await screen.findByRole('button', { name: /Omi user notes/ }));
  expect(screen.getByRole('link', { name: 'Download' })).toHaveAttribute(
    'href',
    brand.links.download,
  );
  expect(screen.getByRole('link', { name: 'Feedback' })).toHaveAttribute(
    'href',
    brand.links.feedback,
  );
  expect(screen.queryByRole('link', { name: 'Discord' })).not.toBeInTheDocument();
});

test('goal copy is branded but the actual submitted user title stays unchanged', async () => {
  const create = vi.fn(async () => ({ id: 'new-goal' }));
  render(<GoalComposer open onOpenChange={vi.fn()} onCreate={create} />);
  expect(
    screen.getByText('Harbor tracks progress against it as you go.'),
  ).toBeInTheDocument();
  const input = screen.getByPlaceholderText('Read 12 books this year');
  fireEvent.change(input, { target: { value: 'Organize my Omi notes' } });
  fireEvent.keyDown(input, { key: 'Enter' });
  expect(create).toHaveBeenCalledWith({
    title: 'Organize my Omi notes',
    target_value: 1,
  });
});

test('support and footer use declared contact destinations without an upstream support iframe', () => {
  const { container } = render(
    <>
      <HelpPage />
      <Footer />
    </>,
  );
  expect(screen.getByRole('heading', { name: 'Harbor support' })).toBeInTheDocument();
  expect(container.querySelector('iframe')).toBeNull();
  expect(screen.getByText(brand.tagline)).toBeInTheDocument();
  for (const link of container.querySelectorAll('a')) {
    expect(link.href).toMatch(
      /^https:\/\/(?:web|api|docs|help|feedback)\.harbor\.invalid|^mailto:help@harbor\.invalid$/,
    );
  }
  expect(screen.queryByRole('link', { name: 'Community' })).not.toBeInTheDocument();
});

test('recording and activity indicators display the manifest image and honor pause', () => {
  const { container, rerender } = render(<OmiOrb state="listening" level={0.8} paused />);
  expect(screen.getByRole('img', { name: 'Harbor' })).toHaveAttribute(
    'data-animated',
    'false',
  );
  expect(container.querySelector('img')).toHaveAttribute('src', '/logo.png');
  expect(container.querySelector('svg')).toBeNull();
  rerender(<OmiPulseMark size={32} active label="Harbor live" testId="recorder" />);
  expect(screen.getByRole('img', { name: 'Harbor live' })).toHaveAttribute(
    'data-active',
    'true',
  );
});
