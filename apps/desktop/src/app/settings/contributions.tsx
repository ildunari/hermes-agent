import { Slot } from '@/contrib/react/slot'

/** Plugin-owned sections appended to Settings ▸ Appearance. */
export const APPEARANCE_SETTINGS_AREA = 'settings.appearance.sections'

export function AppearanceSettingsContributions() {
  return <Slot area={APPEARANCE_SETTINGS_AREA} variant="pane" />
}
