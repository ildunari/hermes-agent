import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { BrowserController } from './browser-controller'
import {
  $browserTabs,
  $foregroundBrowserTabId,
  bindAutomationTask,
  clearBrowserTabs,
  createBrowserTab
} from './browser-store'
import {
  $browserSupervision,
  clearBrowserSupervision,
  requestLocalBrowserControl,
  superviseBrowserTask
} from './browser-supervision'

const geometry = { height: 600, width: 900, x: 0, y: 0 }
const revokeLocal = vi.fn(async () => ({ ok: true, retired: true }))
const stopAndClose = vi.fn(async () => ({ ok: true, retired: true }))

async function clickAndFlush(button: HTMLElement) {
  await act(async () => {
    fireEvent.click(button)
    await Promise.resolve()
    await Promise.resolve()
  })
}

function setup() {
  const controlled = createBrowserTab({ geometry, profile: 'coding', url: 'https://private.example', workspaceId: 'w' })

  const foreground = createBrowserTab({
    foreground: true,
    geometry,
    profile: 'coding',
    url: 'https://foreground.example',
    workspaceId: 'w'
  })

  const binding = bindAutomationTask('task-opaque', controlled.id)

  superviseBrowserTask(binding, {
    operation: 'navigate',
    ownerId: 'owner-opaque',
    profile: 'coding',
    sessionId: 'session-opaque'
  })

  return { binding, controlled, foreground }
}

describe('trusted browser controller chrome', () => {
  beforeEach(() => {
    clearBrowserTabs()
    clearBrowserSupervision()
    revokeLocal.mockClear()
    stopAndClose.mockClear()
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { browserGuest: { revokeLocal, stopAndClose } }
    })
  })

  afterEach(() => {
    act(() => {
      clearBrowserTabs()
      clearBrowserSupervision()
    })
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('renders redacted trusted identity and pauses without changing foreground focus', async () => {
    const { controlled, foreground } = setup()

    render(<BrowserController guestGeneration="guest-opaque" tab={controlled} />)

    const controllerText = screen.getByRole('region', { name: 'Trusted browser controls' }).textContent ?? ''

    expect(controllerText).toContain('owner-opaque')
    expect(controllerText).toContain('session-opaque')
    expect(controllerText).toContain('navigate')
    expect(screen.queryByText('https://private.example')).toBeNull()

    await clickAndFlush(screen.getByRole('button', { name: 'Pause agent control' }))

    await waitFor(() => expect($browserSupervision.get()['task-opaque'].state).toBe('paused'))
    expect(revokeLocal).toHaveBeenCalledWith({
      guestGeneration: 'guest-opaque',
      tabId: controlled.id,
      taskGeneration: 1,
      taskId: 'task-opaque'
    })
    expect($foregroundBrowserTabId.get()).toBe(foreground.id)
    expect(screen.getByLabelText('Recent control activity').textContent).toContain('pause')
    await waitFor(() => expect(window.document.activeElement).toBe(screen.getByRole('button', { name: 'Hand back to agent' })))
  })

  it('hands back through a fresh tab identity without stealing foreground focus', async () => {
    const { binding, controlled, foreground } = setup()

    await requestLocalBrowserControl(
      {
        guestGeneration: 'guest-opaque',
        tabId: controlled.id,
        taskGeneration: binding.generation,
        taskId: binding.taskId
      },
      'paused',
      revokeLocal
    )
    render(<BrowserController guestGeneration="guest-opaque" tab={controlled} />)

    await clickAndFlush(screen.getByRole('button', { name: 'Hand back to agent' }))

    await waitFor(() => expect($browserSupervision.get()['task-opaque'].state).toBe('handing-back'))
    expect($browserSupervision.get()['task-opaque'].tabId).not.toBe(controlled.id)
    expect($browserSupervision.get()['task-opaque'].generation).toBeGreaterThan(binding.generation)
    expect($foregroundBrowserTabId.get()).toBe(foreground.id)
  })

  it('closes the exact tab only after stop acknowledgement', async () => {
    const { controlled } = setup()
    let acknowledge!: (value: { ok: boolean; retired: boolean }) => void
    stopAndClose.mockImplementationOnce(() => new Promise(resolve => (acknowledge = resolve)))
    render(<BrowserController guestGeneration="guest-opaque" tab={controlled} />)

    fireEvent.click(screen.getByRole('button', { name: 'Stop and close' }))
    expect($browserTabs.get()).toContainEqual(controlled)

    await act(async () => {
      acknowledge({ ok: true, retired: true })
      await Promise.resolve()
      await Promise.resolve()
    })

    await waitFor(() => expect($browserTabs.get()).not.toContainEqual(controlled))
    expect($browserSupervision.get()['task-opaque']).toBeUndefined()
  })

  it('gives an unsupervised user-opened tab trusted close chrome', () => {
    const tab = createBrowserTab({
      foreground: true,
      geometry,
      profile: 'coding',
      url: 'https://user.example',
      workspaceId: 'w'
    })

    render(<BrowserController guestGeneration={null} tab={tab} />)
    fireEvent.click(screen.getByRole('button', { name: 'Stop and close' }))

    expect($browserTabs.get()).toEqual([])
  })
})
