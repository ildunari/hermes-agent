import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { createPluginContext } from '@/contrib/plugin'

import { APPEARANCE_SETTINGS_AREA, AppearanceSettingsContributions } from './contributions'

afterEach(cleanup)

describe('Appearance settings contributions', () => {
  it('mounts plugin settings and removes them when the plugin unloads', () => {
    const disposers: Array<() => void> = []
    const ctx = createPluginContext('settings-test', dispose => disposers.push(dispose))

    ctx.register({
      area: APPEARANCE_SETTINGS_AREA,
      id: 'section',
      render: () => <section>Plugin preferences</section>
    })

    const view = render(<AppearanceSettingsContributions />)
    expect(screen.getByText('Plugin preferences')).toBeTruthy()

    act(() => disposers.forEach(dispose => dispose()))
    expect(screen.queryByText('Plugin preferences')).toBeNull()
    view.unmount()
  })
})
