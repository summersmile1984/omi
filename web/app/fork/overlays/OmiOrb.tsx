'use client';

import type { OmiOrbMotion, OmiOrbState } from '@/lib/omiOrb';
import { BrandActivityMark } from '@/components/fork/BrandActivityMark';

export function OmiOrb({
  state = 'idle',
  motion,
  level,
  size = 46,
  className,
  paused,
}: {
  state?: OmiOrbState;
  motion?: OmiOrbMotion;
  level?: number;
  size?: number;
  seed?: number;
  className?: string;
  paused?: boolean;
}) {
  return (
    <BrandActivityMark
      size={size}
      level={level}
      className={className}
      paused={paused}
      active={state !== 'idle' || (motion !== undefined && motion !== 'mark')}
    />
  );
}
