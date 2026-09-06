'use client';

import { motion, useReducedMotion } from 'framer-motion';
import { productName } from '@/lib/fork/web-profile';

export function BrandActivityMark({
  size,
  level = 0,
  active = false,
  paused = false,
  label,
  className,
  testId,
}: {
  size: number;
  level?: number;
  active?: boolean;
  paused?: boolean;
  label?: string;
  className?: string;
  testId?: string;
}) {
  const reduced = useReducedMotion();
  const pulses = active && !paused && !reduced;
  const strength = Math.min(1, Math.max(0, Number.isFinite(level) ? level : 0));
  return (
    <motion.div
      role="img"
      aria-label={label ?? productName()}
      data-testid={testId}
      data-active={active}
      data-animated={pulses}
      className={className}
      style={{ width: size, height: size, flexShrink: 0 }}
      animate={{ scale: pulses ? [0.96, 1.02 + strength * 0.06, 0.96] : 1 }}
      transition={
        pulses
          ? { duration: 1.5 - strength * 0.4, repeat: Infinity, ease: 'easeInOut' }
          : { duration: 0 }
      }
    >
      <img
        src="/logo.png"
        alt=""
        width={size}
        height={size}
        className="h-full w-full object-contain"
      />
    </motion.div>
  );
}
