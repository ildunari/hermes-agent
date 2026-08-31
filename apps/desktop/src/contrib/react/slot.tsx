import { ContribBoundary, ContribRender } from './boundary'
import { useContributions } from './use-contributions'

export interface SlotProps {
  /** Area id whose contributions render inline, in order. */
  area: string
  /** Compact bars use chip fallbacks; page/settings surfaces use pane. */
  variant?: 'chip' | 'pane'
}

/** Renders a bar area: ordered inline items `[...core, ...plugin]`. */
export function Slot({ area, variant = 'chip' }: SlotProps) {
  const items = useContributions(area)

  if (items.length === 0) {
    return null
  }

  return (
    <>
      {items.map(c => (
        <ContribBoundary id={c.id} key={`${c.source ?? 'core'}:${c.id}`} variant={variant}>
          {c.render && <ContribRender render={c.render} />}
        </ContribBoundary>
      ))}
    </>
  )
}
