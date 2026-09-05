'use client';

import { useParams } from '@tschk/moonshine-next/navigation';
import { ShareExperience } from '@/components/fork/ShareExperience';
import { registerMoonshineRoute } from '@/moonshine/register-client-route';

export default function SharedChatPage() {
  const { token = '' } = useParams();
  return <ShareExperience kind="chat" token={token} />;
}

registerMoonshineRoute('/chat/:token', SharedChatPage, 'root');
