import { BrandImage } from '../../src/renderer/src/components/ui/BrandImage'
import lightLogo from '../assets/logo_light.png'
import darkLogo from '../assets/logo_dark.png'

export function BrandMark({
  className,
  lightSurface = false
}: {
  className?: string
  lightSurface?: boolean
}): React.JSX.Element {
  return (
    <BrandImage
      src={lightSurface ? darkLogo : lightLogo}
      alt=""
      className={className ?? 'h-full w-full object-contain'}
    />
  )
}
