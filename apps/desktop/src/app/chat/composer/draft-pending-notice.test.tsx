import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { I18nProvider } from '@/i18n'

import { DraftPendingNotice } from './draft-pending-notice'

afterEach(cleanup)

describe('DraftPendingNotice', () => {
  it('makes busy composer text visibly distinct from a submitted or queued turn', () => {
    render(
      <I18nProvider configClient={null} initialLocale="en">
        <DraftPendingNotice />
      </I18nProvider>
    )

    expect(screen.getByTestId('composer-draft-pending-notice').textContent).toContain(
      'Draft only — press Send to queue it after the current run.'
    )
  })
})
