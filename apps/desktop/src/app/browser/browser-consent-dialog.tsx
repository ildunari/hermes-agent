import { useStore } from '@nanostores/react'
import { useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { useI18n } from '@/i18n'

import {
  $browserConsentPrompts,
  removeBrowserConsent,
  resolveBrowserConsent,
  startBrowserConsentListener
} from './browser-consent'

const CATEGORY_KEYS = {
  'destructive-action': 'destructiveAction',
  download: 'download',
  'external-handler': 'externalHandler',
  navigation: 'navigation',
  'outbound-pixels': 'outboundPixels',
  permission: 'permission',
  'upload-assignment': 'uploadAssignment',
  'website-submission': 'websiteSubmission'
} as const

export function BrowserConsentDialog() {
  const prompts = useStore($browserConsentPrompts)
  const prompt = prompts[0]
  const { t } = useI18n()
  const copy = t.browserConsent
  const uploadCopy = t.browserUpload
  const denyRef = useRef<HTMLButtonElement | null>(null)
  const [deciding, setDeciding] = useState(false)

  useEffect(() => startBrowserConsentListener(), [])
  useEffect(() => {
    if (!prompt) {return}
    const delay = Math.max(0, prompt.expiresAt - Date.now())
    const timer = window.setTimeout(() => removeBrowserConsent(prompt.consentId), delay)

    return () => window.clearTimeout(timer)
  }, [prompt])
  useEffect(() => setDeciding(false), [prompt?.consentId])

  if (!prompt) {return null}

  const decide = (decision: 'allow' | 'deny' | 'ordinary-for-task') => {
    if (deciding) {return}
    setDeciding(true)

    void resolveBrowserConsent(prompt.consentId, decision).finally(() => setDeciding(false))
  }

  const returnFocusToTrustedChrome = (event: Event) => {
    event.preventDefault()

    const controller = Array.from(window.document.querySelectorAll<HTMLElement>('[data-browser-controller]'))
      .find(candidate => candidate.dataset.browserController === prompt.taskId)

    controller?.focus()
  }

  return (
    <Dialog key={prompt.consentId} onOpenChange={open => { if (!open) {decide('deny')} }} open>
      <DialogContent
        aria-describedby="browser-consent-description"
        className="max-w-md"
        data-browser-consent={prompt.consentId}
        onCloseAutoFocus={returnFocusToTrustedChrome}
        onOpenAutoFocus={event => {
          event.preventDefault()
          denyRef.current?.focus()
        }}
        showCloseButton={false}
      >
        <DialogHeader>
          <DialogTitle>{copy.title}</DialogTitle>
          <DialogDescription id="browser-consent-description">
            {copy.categories[CATEGORY_KEYS[prompt.category]]}
          </DialogDescription>
        </DialogHeader>

        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-2 rounded-lg border border-border/60 bg-muted/30 p-3 text-sm">
          <dt className="text-muted-foreground">{copy.site}</dt>
          <dd className="break-all font-medium">{prompt.site}</dd>
          <dt className="text-muted-foreground">{copy.profile}</dt><dd className="break-all">{prompt.profile}</dd>
          <dt className="text-muted-foreground">{copy.task}</dt><dd className="break-all font-mono text-xs">{prompt.taskId}</dd>
          <dt className="text-muted-foreground">{copy.tab}</dt><dd className="break-all font-mono text-xs">{prompt.tabId}</dd>
          <dt className="text-muted-foreground">{copy.taskGeneration}</dt><dd>{prompt.taskGeneration}</dd>
          <dt className="text-muted-foreground">{copy.browserGeneration}</dt><dd className="break-all font-mono text-xs">{prompt.guestGeneration}</dd>
          {prompt.permission && <><dt className="text-muted-foreground">{copy.permission}</dt><dd>{prompt.permission}</dd></>}
          {prompt.category === 'download' && prompt.filename && <><dt className="text-muted-foreground">{copy.filename}</dt><dd className="break-all">{prompt.filename}</dd></>}
          {prompt.scheme && <><dt className="text-muted-foreground">{copy.scheme}</dt><dd className="break-all">{prompt.scheme}</dd></>}
          {prompt.detail && <><dt className="text-muted-foreground">{copy.detail}</dt><dd className="break-words">{prompt.detail}</dd></>}
          {prompt.navigationPolicy && <>
            <dt className="text-muted-foreground">{copy.classification}</dt>
            <dd>{prompt.navigationPolicy.classification === 'ordinary'
              ? copy.classificationOrdinary
              : prompt.navigationPolicy.classification === 'sensitive'
                ? copy.classificationSensitive
                : copy.classificationUnknown}</dd>
            <dt className="text-muted-foreground">{copy.policyReasons}</dt>
            <dd className="break-words font-mono text-xs">{prompt.navigationPolicy.reasonCodes.join(', ')}</dd>
            <dt className="text-muted-foreground">{copy.policySource}</dt>
            <dd className="break-words font-mono text-xs">
              {prompt.navigationPolicy.provenance.map(row => row.source).join(', ')} · {prompt.navigationPolicy.policyVersion} · {prompt.navigationPolicy.pslVersion}
            </dd>
          </>}
          {prompt.category === 'upload-assignment' && prompt.upload && <>
            <dt className="text-muted-foreground">{uploadCopy.consent.sourceLabel}</dt>
            <dd>{uploadCopy.consent.source}</dd>
            <dt className="text-muted-foreground">{uploadCopy.consent.destination}</dt>
            <dd className="break-all">{prompt.upload.destinationOrigin}</dd>
            <dt className="text-muted-foreground">{uploadCopy.consent.form}</dt>
            <dd className="break-words">
              {prompt.upload.formLabel || '—'} ({prompt.upload.formMethod.toUpperCase()} {prompt.upload.formActionOrigin})
            </dd>
            <dt className="text-muted-foreground">{uploadCopy.consent.input}</dt>
            <dd className="break-words">{prompt.upload.inputLabel || '—'}</dd>
            <dt className="text-muted-foreground">{uploadCopy.consent.mode}</dt>
            <dd>{prompt.upload.mode}</dd>
            <dt className="text-muted-foreground">{uploadCopy.consent.accept}</dt>
            <dd className="break-words">{prompt.upload.accept || uploadCopy.empty}</dd>
            <dt className="text-muted-foreground">{uploadCopy.consent.aggregate}</dt>
            <dd>{prompt.upload.aggregateSize} {uploadCopy.bytes}</dd>
            <dt className="text-muted-foreground">{uploadCopy.consent.rename}</dt>
            <dd className="break-words">
              {prompt.upload.files.some(file => file.originalDisplayName && file.originalDisplayName !== file.displayName)
                ? prompt.upload.files
                    .filter(file => file.originalDisplayName && file.originalDisplayName !== file.displayName)
                    .map(file => `${file.originalDisplayName} → ${file.displayName}`)
                    .join(', ')
                : uploadCopy.none}
            </dd>
            <dt className="text-muted-foreground">{uploadCopy.consent.files}</dt>
            <dd>
              <ul className="space-y-1">
                {prompt.upload.files.map((file, index) => (
                  <li className="break-words" key={`${index}-${file.displayName}`}>
                    {file.displayName} — {file.mimeType}, {file.size} {uploadCopy.bytes}
                  </li>
                ))}
              </ul>
            </dd>
            <dt className="text-muted-foreground">{uploadCopy.consent.immediate}</dt>
            <dd>{prompt.upload.immediateSubmissionPossible ? '✓' : '—'}</dd>
          </>}
          {prompt.recipient && <><dt className="text-muted-foreground">{copy.recipient}</dt><dd className="break-all">{prompt.recipient}</dd></>}
          {prompt.purpose && <><dt className="text-muted-foreground">{copy.purpose}</dt><dd className="break-words">{prompt.purpose}</dd></>}
          {prompt.category === 'outbound-pixels' && prompt.captureScope === 'viewport' && Number.isSafeInteger(prompt.documentGeneration) && (
            <><dt className="text-muted-foreground">{copy.captureScope}</dt><dd>{copy.viewportDocumentScope(prompt.documentGeneration!)}</dd></>
          )}
          {prompt.category === 'outbound-pixels' && prompt.retention === 'memory-only-transient' && (
            <><dt className="text-muted-foreground">{copy.retention}</dt><dd>{copy.memoryOnlyRetention}</dd></>
          )}
          <dt className="text-muted-foreground">{copy.operation}</dt>
          <dd className="break-all font-mono text-xs">{prompt.operationId}</dd>
        </dl>

        {prompt.category === 'outbound-pixels' && (
          <p className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm leading-relaxed" role="alert">
            {copy.pixelWarning}
          </p>
        )}

        {prompt.category === 'upload-assignment' && prompt.upload && (
          <p className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm leading-relaxed" role="alert">
            {uploadCopy.consent.warning}
          </p>
        )}

        {prompt.category === 'navigation' && (
          <p className="rounded-md border border-border/60 bg-muted/30 p-3 text-sm leading-relaxed">
            {copy.navigationGateWarning}
          </p>
        )}

        <p className="text-xs leading-relaxed text-muted-foreground">{copy.scopeWarning} {copy.expires}</p>

        <DialogFooter className="flex-row justify-end">
          <Button disabled={deciding} onClick={() => decide('deny')} ref={denyRef} variant="outline">{copy.deny}</Button>
          {prompt.category === 'navigation' && prompt.navigationPolicy?.exceptionEligible && (
            <Button disabled={deciding} onClick={() => decide('ordinary-for-task')} variant="outline">{copy.treatOrdinary}</Button>
          )}
          <Button disabled={deciding} onClick={() => decide('allow')}>{copy.allowOnce}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
