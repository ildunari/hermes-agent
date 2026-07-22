import type { BrowserUploadConsentDetail } from './browser-consent'
import type { BrowserPendingUploadChooser, BrowserUploadAssignmentFile } from './browser-guest-security'

export function buildBrowserUploadConsentDetail(
  chooser: Readonly<BrowserPendingUploadChooser>,
  files: readonly BrowserUploadAssignmentFile[]
): BrowserUploadConsentDetail {
  return {
    accept: chooser.accept,
    aggregateSize: files.reduce((sum, file) => sum + file.size, 0),
    destinationOrigin: chooser.origin,
    files: files.map(({ displayName, mimeType, originalDisplayName, size }) => ({
      displayName,
      mimeType,
      ...(originalDisplayName && originalDisplayName !== displayName ? { originalDisplayName } : {}),
      size
    })),
    formActionOrigin: chooser.formActionOrigin,
    formLabel: chooser.formLabel,
    formMethod: chooser.formMethod,
    immediateSubmissionPossible: true,
    inputLabel: chooser.inputLabel || chooser.inputName,
    mode: chooser.mode,
    source: 'studio-session-artifact'
  }
}
