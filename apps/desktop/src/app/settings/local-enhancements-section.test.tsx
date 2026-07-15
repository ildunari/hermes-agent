import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { I18nProvider } from '@/i18n'

import { LocalEnhancementsSection } from './local-enhancements-section'

describe('LocalEnhancementsSection', () => {
  it('labels local carry and can collapse its controls', () => {
    render(
      <I18nProvider configClient={null} initialLocale="en">
        <LocalEnhancementsSection>
          <div>Custom control</div>
        </LocalEnhancementsSection>
      </I18nProvider>
    )

    const toggle = screen.getByRole('button', { name: /Local Enhancements/ })

    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(screen.queryByText('Custom control')).not.toBeNull()

    fireEvent.click(toggle)

    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByText('Custom control')).toBeNull()
  })
})
