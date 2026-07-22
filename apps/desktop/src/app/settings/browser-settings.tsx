import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { ErrorState } from '@/components/ui/error-state'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { useI18n } from '@/i18n'
import { Clock, Globe, RefreshCw, Trash2 } from '@/lib/icons'
import { $activeProfile, normalizeProfileKey } from '@/store/profile'

import { reseedBrowserPersistence, resetBrowserWorkspaceDetailed, setBrowserRestoreEnabled } from '../browser/browser-persistence'

import { ListRow, LoadingState, SectionHeading, SettingsContent } from './primitives'

interface BrowserSettingsState {
  degraded: boolean
  history: BrowserVisitPersistedRow[]
  origins: BrowserOriginPersistedRow[]
  permissions: BrowserPermissionPersistedRow[]
  restoreEnabled: boolean
  workspaces: string[]
}

interface ActionResult {
  failedScopes?: string[]
  ok: boolean
}

const DURABLE_PERMISSIONS = [
  'notifications', 'geolocation', 'media', 'clipboard-read', 'fullscreen', 'pointerLock',
  'idle-detection', 'display-capture', 'speaker-selection', 'midi', 'midiSysex',
  'bluetooth', 'hid', 'serial', 'usb'
] as const

export function BrowserSettings() {
  const { t } = useI18n()
  const profile = normalizeProfileKey(useStore($activeProfile))
  const [state, setState] = useState<BrowserSettingsState | null>(null)
  const [busy, setBusy] = useState(false)
  const [permissionOrigin, setPermissionOrigin] = useState('')
  const [permissionName, setPermissionName] = useState('notifications')
  const copy = t.settings.browser

  const load = useCallback(async () => {
    const [snapshot, history, origins, permissions] = await Promise.all([
      window.hermesDesktop.browserState.snapshot({ profile }),
      window.hermesDesktop.browserState.history({ limit: 500, profile }),
      window.hermesDesktop.browserState.origins({ profile }),
      window.hermesDesktop.browserState.permissions({ limit: 5_000, profile })
    ])
    const workspaces = new Set([
      ...snapshot.descriptors.map(row => row.workspaceId),
      ...history.rows.map(row => row.workspaceId)
    ])
    setState({
      degraded: snapshot.degraded || history.degraded || origins.degraded || permissions.degraded,
      history: history.rows,
      origins: origins.rows,
      permissions: permissions.rows,
      restoreEnabled: snapshot.restoreEnabled,
      workspaces: [...workspaces].sort()
    })
  }, [profile])

  useEffect(() => {setState(null); void load()}, [load])

  const historyByOrigin = useMemo(() => {
    const rows = new Map<string, BrowserVisitPersistedRow>()
    for (const row of state?.history ?? []) {rows.set(row.origin, row)}
    return rows
  }, [state?.history])

  const act = async (action: () => Promise<ActionResult | boolean>) => {
    setBusy(true)
    try {
      const raw = await action()
      const result = typeof raw === 'boolean' ? { ok: raw } : raw
      if (!result.ok) {
        window.alert(result.failedScopes?.length ? copy.partialFailure(result.failedScopes.join(', ')) : copy.actionFailed)
      }
      await load()
    } finally {setBusy(false)}
  }

  const repair = async (mode: 'reset-metadata' | 'retry'): Promise<ActionResult> => {
    const result = await window.hermesDesktop.browserState.repair({ mode, profile })
    const persistence = result.ok && reseedBrowserPersistence(profile, result)
    return {
      failedScopes: [
        !result.metadata && copy.metadataScope,
        !result.activity && copy.activityScope,
        !persistence && copy.workspaceScope
      ].filter(Boolean) as string[],
      ok: result.ok && persistence
    }
  }

  const clearAll = async (): Promise<ActionResult> => {
    const result = await window.hermesDesktop.browserState.clearBrowsingData({ profile })
    return {
      failedScopes: [!result.siteData && copy.siteDataScope, !result.permissions && copy.permissionsScope, !result.metadata && copy.metadataScope].filter(Boolean) as string[],
      ok: result.ok
    }
  }

  if (!state) {return <LoadingState label={copy.loading} />}

  if (state.degraded) {
    return (
      <SettingsContent>
        <ErrorState className="pt-16" description={copy.degradedDescription} title={copy.degradedTitle}>
          <div className="flex flex-wrap justify-center gap-2">
            <Button disabled={busy} onClick={() => void act(() => repair('retry'))} variant="secondary">{copy.retry}</Button>
            <Button disabled={busy} onClick={() => {
              if (window.confirm(copy.resetMetadataConfirm)) {void act(() => repair('reset-metadata'))}
            }} variant="outline">{copy.resetMetadata}</Button>
            <Button disabled={busy} onClick={() => void act(async () => {
              const result = await window.hermesDesktop.browserState.exportQuarantinedMetadata({ profile })
              return result.canceled ? true : result.ok
            })} variant="outline">{copy.exportMetadata}</Button>
            <Button disabled={busy} onClick={() => {
              if (window.confirm(copy.clearAllDataConfirm)) {
                void act(async () => {
                  const repaired = await repair('reset-metadata')
                  const cleared = await clearAll()
                  return { failedScopes: [...(repaired.failedScopes ?? []), ...(cleared.failedScopes ?? [])], ok: repaired.ok && cleared.ok }
                })
              }
            }} variant="destructive">{copy.clearBrowsingData}</Button>
          </div>
        </ErrorState>
      </SettingsContent>
    )
  }

  return (
    <SettingsContent>
      <SectionHeading icon={Globe} title={copy.title} />
      <p className="mb-5 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">{copy.intro(profile)}</p>

      <ListRow
        action={<Switch aria-label={copy.restoreTitle} checked={state.restoreEnabled} disabled={busy} onCheckedChange={enabled => void act(() => setBrowserRestoreEnabled(profile, enabled))} size="xs" />}
        description={copy.restoreDescription}
        title={copy.restoreTitle}
      />

      <SectionHeading icon={Clock} title={copy.historyTitle} />
      {state.origins.length === 0 ? (
        <p className="py-3 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">{copy.historyEmpty}</p>
      ) : state.origins.map(row => {
        const visit = historyByOrigin.get(row.origin)
        return (
          <ListRow
            action={<Button disabled={busy} onClick={() => {
              if (window.confirm(copy.clearSiteConfirm(row.origin))) {
                void act(async () => {
                  const result = await window.hermesDesktop.browserState.clearSiteData({ origin: row.origin, profile })
                  return {
                    failedScopes: [!result.siteData && copy.siteDataScope, !result.permissions && copy.permissionsScope].filter(Boolean) as string[],
                    ok: result.ok
                  }
                })
              }
            }} size="xs" variant="text">{copy.clearSite}</Button>}
            description={visit ? (visit.redactionClass === 'sensitive' ? copy.redacted : visit.title || visit.url) : copy.originMetadataOnly}
            hint={row.origin}
            key={row.origin}
            title={row.origin}
          />
        )
      })}
      {state.history.length > 0 && (
        <Button disabled={busy} onClick={() => void act(async () => (await window.hermesDesktop.browserState.clearMetadata({ history: true, profile })).ok)} size="sm" variant="outline">
          {copy.clearHistory}
        </Button>
      )}

      <SectionHeading icon={Globe} title={copy.permissionsTitle} />
      <p className="mb-2 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">{copy.permissionsDescription}</p>
      <div className="mb-3 grid gap-2 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_auto_auto]">
        <Input aria-label={copy.permissionOrigin} disabled={busy} onChange={event => setPermissionOrigin(event.target.value)} placeholder="https://example.com" size="sm" value={permissionOrigin} />
        <select
          aria-label={copy.permissionName}
          className="h-8 rounded-md border border-(--ui-border) bg-(--ui-bg) px-2 text-sm"
          disabled={busy}
          onChange={event => setPermissionName(event.target.value)}
          value={permissionName}
        >
          {DURABLE_PERMISSIONS.map(permission => <option key={permission} value={permission}>{permission}</option>)}
        </select>
        <Button disabled={busy || !permissionOrigin || !permissionName} onClick={() => void act(async () => (await window.hermesDesktop.browserState.setPermission({ decision: 'allow', origin: permissionOrigin, permission: permissionName, persistence: 'durable', profile })).ok)} size="sm">{copy.allow}</Button>
        <Button disabled={busy || !permissionOrigin || !permissionName} onClick={() => void act(async () => (await window.hermesDesktop.browserState.setPermission({ decision: 'deny', origin: permissionOrigin, permission: permissionName, persistence: 'durable', profile })).ok)} size="sm" variant="outline">{copy.deny}</Button>
      </div>
      {state.permissions.length === 0 && <p className="py-2 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">{copy.permissionsEmpty}</p>}
      {state.permissions.map(row => (
        <ListRow
          action={<Button disabled={busy} onClick={() => void act(async () => (await window.hermesDesktop.browserState.removePermission({ origin: row.origin, permission: row.permission, profile })).ok)} size="xs" variant="text">{copy.removePermission}</Button>}
          description={row.decision === 'allow' ? copy.allowed : copy.denied}
          hint={row.origin}
          key={`${row.origin}\0${row.permission}`}
          title={row.permission}
        />
      ))}

      {state.workspaces.length > 0 && <SectionHeading icon={RefreshCw} title={copy.workspaceTitle} />}
      {state.workspaces.length > 0 && <p className="mb-2 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">{copy.workspaceDescription}</p>}
      {state.workspaces.map(workspaceId => (
        <ListRow
          action={<div className="flex flex-wrap justify-end gap-2">
            {[false, true].map(includeHistory => (
              <Button disabled={busy} key={String(includeHistory)} onClick={() => {
                if (window.confirm(copy.resetConfirm)) {
                  void act(async () => {
                    const result = await resetBrowserWorkspaceDetailed(profile, workspaceId, includeHistory)
                    return {
                      failedScopes: [!result.state && copy.workspaceScope, !result.activity && copy.activityScope].filter(Boolean) as string[],
                      ok: result.ok
                    }
                  })
                }
              }} size="xs" variant="outline">{includeHistory ? copy.resetWorkspaceAndHistory : copy.resetWorkspace}</Button>
            ))}
          </div>}
          hint={workspaceId}
          key={workspaceId}
          title={workspaceId}
        />
      ))}

      <SectionHeading icon={Trash2} title={copy.clearAllData} />
      <ListRow
        action={<Button disabled={busy} onClick={() => {
          if (window.confirm(copy.clearAllDataConfirm)) {void act(clearAll)}
        }} size="sm" variant="destructive">{copy.clearAllData}</Button>}
        description={copy.clearAllDataDescription}
        title={copy.clearAllData}
      />
    </SettingsContent>
  )
}
