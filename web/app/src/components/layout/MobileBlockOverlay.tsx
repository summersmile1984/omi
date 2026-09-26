'use client';

import { useEffect, useState } from 'react';
import { usePathname } from '@tschk/moonshine-next/navigation';
import { productName, webPresentation, webProfile } from '@/lib/fork/web-profile';

export function MobileBlockOverlay() {
  const pathname = usePathname();
  const [visible, setVisible] = useState(false);
  const key = `${webProfile().name}:desktop-notice-dismissed`;
  useEffect(() => {
    const update = () =>
      setVisible(window.innerWidth < 768 && sessionStorage.getItem(key) !== 'true');
    update();
    window.addEventListener('resize', update);
    return () => window.removeEventListener('resize', update);
  }, [key]);
  if (!visible || ['/record/popout', '/apps'].some((path) => pathname?.startsWith(path)))
    return null;
  const { links } = webPresentation();
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Desktop experience"
      className="fixed inset-0 z-[9999] flex flex-col items-center justify-center gap-6 bg-black p-6 text-center text-white"
    >
      <img src="/logo.png" alt={productName()} width={96} height={96} />
      <h1 className="text-2xl font-semibold">{productName()} on the web</h1>
      <p className="max-w-sm text-neutral-300">
        A larger screen offers the full workspace. You can continue here or download the
        macOS app for your Mac.
      </p>
      <button
        className="rounded-lg bg-white px-5 py-3 text-black"
        onClick={() => {
          sessionStorage.setItem(key, 'true');
          setVisible(false);
        }}
      >
        Continue to web
      </button>
      <a href={links.download} className="underline">
        Download for macOS
      </a>
    </div>
  );
}
