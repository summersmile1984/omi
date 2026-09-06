'use client';

import { productName, webPresentation } from '@/lib/fork/web-profile';
import { registerMoonshineRoute } from '@/moonshine/register-client-route';

export default function HelpPage() {
  const { support_email, links } = webPresentation();
  return (
    <main className="mx-auto w-full max-w-3xl space-y-6 px-6 py-10">
      <h1 className="text-2xl font-semibold text-text-primary">
        {productName()} support
      </h1>
      <p className="text-text-secondary">
        Find documentation, report an issue, or contact the team.
      </p>
      <nav
        aria-label="Support"
        className="flex flex-col items-start gap-4 text-text-primary"
      >
        <a href={links.docs} className="underline">
          Documentation
        </a>
        <a href={links.help} className="underline">
          Help and issues
        </a>
        <a href={links.feedback} className="underline">
          Share feedback
        </a>
        <a href={`mailto:${support_email}`} className="underline">
          {support_email}
        </a>
      </nav>
    </main>
  );
}

registerMoonshineRoute('/help', HelpPage, 'authenticated');
