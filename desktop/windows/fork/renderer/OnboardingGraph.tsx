import { Suspense, type ComponentProps } from 'react'
import { BrainGraph as Graph } from '../../src/renderer/src/components/graph/BrainGraph'

// Canvas font readiness may suspend. Keep that asynchronous visual subtree
// inside its own boundary so onboarding decisions still commit and persist.
export function BrainGraph(props: ComponentProps<typeof Graph>): React.JSX.Element {
  return (
    <Suspense
      fallback={
        <p role="status" className="text-sm text-white/50">
          Your profile map is loading. You can continue setup.
        </p>
      }
    >
      <Graph {...props} />
    </Suspense>
  )
}
