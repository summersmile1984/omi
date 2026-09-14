'use client';

import { useParams } from '@tschk/moonshine-next/navigation';
import { ShareExperience } from '@/components/fork/ShareExperience';
import { registerMoonshineRoute } from '@/moonshine/register-client-route';

export default function SharedTasksPage() {
  const { token = '' } = useParams();
  return <ShareExperience kind="tasks" token={token} />;
}

registerMoonshineRoute('/tasks/:token', SharedTasksPage, 'root');
