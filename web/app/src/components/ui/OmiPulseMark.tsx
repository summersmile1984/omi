'use client';

import { BrandActivityMark } from '@/components/fork/BrandActivityMark';

export function OmiPulseMark(props: {
  size: number;
  level?: number;
  active?: boolean;
  label?: string;
  testId?: string;
}) {
  return <BrandActivityMark {...props} />;
}
