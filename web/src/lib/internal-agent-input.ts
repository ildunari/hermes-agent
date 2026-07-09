const PROCESS_NOTIFICATION_RE = /^\[IMPORTANT: Background process [\s\S]*\]$/;
const ASYNC_DELEGATION_NOTIFICATION_RE = /^\[ASYNC DELEGATION(?: BATCH)? COMPLETE\s+—\s+[\s\S]+$/;

export function isBackgroundProcessAgentInput(text: string): boolean {
  return PROCESS_NOTIFICATION_RE.test(text.trim());
}

export function isAsyncDelegationAgentInput(text: string): boolean {
  return ASYNC_DELEGATION_NOTIFICATION_RE.test(text.trim());
}

export function isInternalAgentInput(text: string): boolean {
  return isBackgroundProcessAgentInput(text) || isAsyncDelegationAgentInput(text);
}
