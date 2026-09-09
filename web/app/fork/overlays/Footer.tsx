'use client';

import { productName, webPresentation } from '@/lib/fork/web-profile';

export function Footer() {
  const { tagline, support_email, links } = webPresentation();
  return (
    <footer className="border-t border-neutral-800 bg-black px-6 py-10 text-neutral-300">
      <div className="mx-auto flex max-w-screen-xl flex-wrap items-start justify-between gap-8">
        <div className="space-y-3">
          <a
            href={links.website}
            className="flex items-center gap-3 text-lg font-semibold text-white"
          >
            <img src="/logo.png" alt="" width={32} height={32} />
            {productName()}
          </a>
          <p>{tagline}</p>
          <a className="block underline" href={`mailto:${support_email}`}>
            {support_email}
          </a>
        </div>
        <nav aria-label="Resources" className="flex flex-wrap gap-5 text-sm">
          {Object.entries({
            'Download for macOS': links.download,
            Documentation: links.docs,
            Help: links.help,
            Feedback: links.feedback,
            Privacy: links.privacy,
            Terms: links.terms,
            ...(links.community ? { Community: links.community } : {}),
          }).map(([label, href]) => (
            <a key={label} href={href} className="underline hover:text-white">
              {label}
            </a>
          ))}
        </nav>
      </div>
    </footer>
  );
}
