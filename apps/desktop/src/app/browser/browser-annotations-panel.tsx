import { useCallback, useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { useI18n } from '@/i18n'
import { Download, RefreshCw } from '@/lib/icons'

import type { BrowserTab } from './browser-store'

type ProjectionHealth = 'ambiguous' | 'resolved' | 'shifted' | 'stale' | 'unsupported'
type AnnotationKind = 'agent-marker' | 'comment' | 'drawing' | 'element' | 'region' | 'text'

const ANNOTATION_KINDS = new Set<AnnotationKind>(['agent-marker', 'comment', 'drawing', 'element', 'region', 'text'])

interface AnnotationRecord {
  annotationId: string
  kind: AnnotationKind
  revision: number
  scope: {
    browserWorkspaceId: string
    documentGenerationId: string
    profileId: string
    tabId: string
  }
  status: 'dismissed' | 'open' | 'resolved'
}

interface AnnotationRow extends AnnotationRecord {
  externalLabel: number
  health: ProjectionHealth
  projectedDocumentGeneration: number
}

interface BrowserAnnotationsPanelProps {
  guestGeneration: string | null
  tab: BrowserTab
}

function isRecord(value: unknown): value is AnnotationRecord {
  if (!value || typeof value !== 'object') {return false}
  const row = value as Partial<AnnotationRecord>
  const scope = row.scope

  return (
    typeof row.annotationId === 'string' && row.annotationId.length > 0 &&
    typeof row.kind === 'string' && ANNOTATION_KINDS.has(row.kind as AnnotationKind) &&
    Number.isSafeInteger(row.revision) && (row.revision ?? 0) > 0 &&
    (row.status === 'open' || row.status === 'resolved' || row.status === 'dismissed') &&
    Boolean(scope) && typeof scope?.browserWorkspaceId === 'string' &&
    typeof scope.documentGenerationId === 'string' && typeof scope.profileId === 'string' &&
    typeof scope.tabId === 'string'
  )
}

export function isAnnotationActionCurrent(
  row: Pick<AnnotationRow, 'health' | 'projectedDocumentGeneration'>,
  documentGeneration: number
): boolean {
  return (
    (row.health === 'resolved' || row.health === 'shifted') &&
    row.projectedDocumentGeneration === documentGeneration
  )
}

export function BrowserAnnotationsPanel({ guestGeneration, tab }: BrowserAnnotationsPanelProps) {
  const { t } = useI18n()
  const copy = t.browserAnnotations
  const [rows, setRows] = useState<AnnotationRow[]>([])
  const [state, setState] = useState<'error' | 'idle' | 'loading' | 'stale'>('idle')
  const requestRef = useRef(0)
  const recordsRef = useRef<AnnotationRecord[]>([])
  const generationRef = useRef(guestGeneration)
  generationRef.current = guestGeneration

  const refresh = useCallback(async () => {
    const request = ++requestRef.current

    if (!guestGeneration || tab.private) {
      recordsRef.current = []
      setRows([])
      setState('idle')

      return
    }

    setState('loading')

    try {
      const records = await window.hermesDesktop.api<unknown>({
        path: `/api/browser/annotations?profile=${encodeURIComponent(tab.profile)}&workspace_id=${encodeURIComponent(tab.workspaceId)}`,
        profile: tab.profile
      })

      if (request !== requestRef.current || generationRef.current !== guestGeneration) {return}

      const viewport = await window.hermesDesktop.browserGuest.report({
        generation: guestGeneration,
        kind: 'viewport',
        tabId: tab.id
      })

      if (!viewport.ok || request !== requestRef.current || generationRef.current !== guestGeneration) {
        setRows([])
        setState('stale')

        return
      }

      const current = Array.isArray(records)
        ? records.filter(isRecord).filter(record =>
            record.scope.profileId === tab.profile &&
            record.scope.browserWorkspaceId === tab.workspaceId &&
            record.scope.tabId === tab.id &&
            record.scope.documentGenerationId === String(viewport.documentGeneration)
          )
        : null

      if (!current) {throw new Error('invalid-annotation-list')}

      const projected = await window.hermesDesktop.browserGuest.resolveAnnotations({
        generation: guestGeneration,
        records: current,
        tabId: tab.id,
        workspaceId: tab.workspaceId
      })

      if (
        !projected.ok || projected.documentGeneration !== viewport.documentGeneration ||
        !projected.projections || request !== requestRef.current || generationRef.current !== guestGeneration
      ) {
        setRows([])
        setState('stale')

        return
      }

      const byId = new Map(current.map(record => [record.annotationId, record]))

      const next = projected.projections.flatMap(projection => {
        const record = byId.get(projection.annotationId)

        return record ? [{
          ...record,
          externalLabel: projection.externalLabel,
          health: projection.health,
          projectedDocumentGeneration: projected.documentGeneration as number
        }] : []
      })

      setRows(next)
      recordsRef.current = current
      setState('idle')
    } catch {
      if (request === requestRef.current && generationRef.current === guestGeneration) {
        recordsRef.current = []
        setRows([])
        setState('error')
      }
    }
  }, [guestGeneration, tab.id, tab.private, tab.profile, tab.workspaceId])

  useEffect(() => {
    void refresh()

    return () => { requestRef.current += 1 }
  }, [refresh, tab.url])

  const resolve = async (row: AnnotationRow) => {
    if (!guestGeneration) {return}
    setState('loading')

    try {
      const current = await window.hermesDesktop.browserGuest.report({
        documentGeneration: row.projectedDocumentGeneration,
        generation: guestGeneration,
        kind: 'viewport',
        tabId: tab.id
      })

      if (
        !current.ok || generationRef.current !== guestGeneration ||
        !isAnnotationActionCurrent(row, current.documentGeneration)
      ) {
        setState('stale')

        return
      }

      await window.hermesDesktop.api({
        body: { expected_revision: row.revision, profile: tab.profile, status: 'resolved' },
        method: 'PATCH',
        path: `/api/browser/annotations/${encodeURIComponent(row.annotationId)}/status`,
        profile: tab.profile
      })
      await refresh()
    } catch {
      setState('error')
    }
  }

  const exportScreenshot = async () => {
    if (!guestGeneration || state === 'loading') {return}
    setState('loading')
    try {
      const result = await window.hermesDesktop.browserGuest.exportAnnotationScreenshot({
        generation: guestGeneration,
        records: recordsRef.current,
        tabId: tab.id,
        workspaceId: tab.workspaceId
      })
      if (!result.ok || generationRef.current !== guestGeneration) {
        setState(result.error?.includes('stale') ? 'stale' : 'error')
        return
      }
      setState('idle')
    } catch {
      setState('error')
    }
  }

  if (tab.private) {return null}

  return (
    <aside
      aria-label={copy.panelLabel}
      className="pointer-events-auto order-first flex h-full w-72 shrink-0 flex-col border-r border-(--ui-stroke-tertiary) bg-(--ui-bg-elevated) text-(--ui-text-primary) [-webkit-app-region:no-drag]"
      data-browser-annotations={tab.id}
    >
      <div className="flex items-center justify-between border-b border-(--ui-stroke-tertiary) px-3 py-2">
        <div><h2 className="text-xs font-semibold">{copy.title}</h2><p className="text-[0.625rem] text-(--ui-text-tertiary)">{copy.subtitle}</p></div>
        <div className="flex items-center gap-1">
          <Button aria-label={copy.exportLabel} disabled={state === 'loading'} onClick={() => void exportScreenshot()} size="icon-sm" variant="ghost">
            <Download aria-hidden />
          </Button>
          <Button aria-label={copy.refreshLabel} disabled={state === 'loading'} onClick={() => void refresh()} size="icon-sm" variant="ghost">
            <RefreshCw aria-hidden className={state === 'loading' ? 'animate-spin' : ''} />
          </Button>
        </div>
      </div>
      {state === 'error' ? <p className="p-3 text-xs text-destructive" role="alert">{copy.error}</p> : null}
      {state === 'stale' ? <p className="p-3 text-xs text-(--ui-text-secondary)" role="status">{copy.stale}</p> : null}
      {state === 'idle' && rows.length === 0 ? <p className="p-3 text-xs text-(--ui-text-tertiary)">{copy.empty}</p> : null}
      <ol className="min-h-0 flex-1 overflow-y-auto p-2">
        {rows.map(row => (
          <li className="mb-2 border border-(--ui-stroke-tertiary) bg-(--ui-bg-secondary) p-2 text-xs" key={row.annotationId}>
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono font-semibold">#{row.externalLabel}</span>
              <span className="text-[0.625rem] text-(--ui-text-tertiary)">{copy.health[row.health]}</span>
            </div>
            <p className="mt-1 text-(--ui-text-secondary)">{copy.kinds[row.kind]} · {copy.statuses[row.status]}</p>
            {row.status === 'open' ? (
              <Button
                className="mt-2"
                disabled={!isAnnotationActionCurrent(row, row.projectedDocumentGeneration) || state === 'loading'}
                onClick={() => void resolve(row)}
                size="sm"
                variant="ghost"
              >{copy.actionResolve}</Button>
            ) : null}
          </li>
        ))}
      </ol>
    </aside>
  )
}
