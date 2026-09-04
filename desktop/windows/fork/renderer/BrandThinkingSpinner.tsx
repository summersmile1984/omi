import { BrandMark } from './BrandMark'
import { profile } from '../native/profile.generated'

export function OmiThinkingSpinner(): React.JSX.Element {
  return (
    <div
      className="mr-auto flex items-center pl-1"
      role="status"
      aria-label={`${profile.personaName} is thinking`}
    >
      <BrandMark className="omi-thinking-spin h-5 w-5" />
    </div>
  )
}
