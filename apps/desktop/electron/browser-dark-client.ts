import crypto from 'node:crypto'
import path from 'node:path'

import wireContract from '../../../hermes_cli/browser_wire_v1.json'

export const WIRE_CONTRACT = wireContract

function methodHashInput(rows = WIRE_CONTRACT.required_methods) {
  return (
    rows
      .map(row => `${row.direction}:${row.name}`)
      .sort()
      .join('\n') + '\n'
  )
}

export function methodSetHash(rows = WIRE_CONTRACT.required_methods) {
  return crypto.createHash('sha256').update(methodHashInput(rows)).digest('hex')
}

/** One memory-only 256-bit identity per running Desktop main process. */
export class DesktopConnectionAssociation {
  readonly connectionId: string

  constructor(randomBytes: (size: number) => Buffer = crypto.randomBytes) {
    const value = randomBytes(32)

    if (!Buffer.isBuffer(value) || value.byteLength !== 32) {
      throw new Error('Desktop connection association requires exactly 256 random bits.')
    }

    this.connectionId = value.toString('base64url')
  }
}

export function appendDesktopAssociation(url: string, connectionId: string, profile: string) {
  const parsed = new URL(url)
  parsed.searchParams.set('connection_id', connectionId)
  parsed.searchParams.set('profile', profile)

  return parsed.toString()
}

export function buildBrowserWsUrl(baseUrl: string, ticket: string) {
  const parsed = new URL(baseUrl)
  parsed.protocol = parsed.protocol === 'https:' ? 'wss:' : 'ws:'
  parsed.pathname = `${parsed.pathname.replace(/\/+$/, '')}/api/ws/browser`
  parsed.search = ''
  parsed.hash = ''
  parsed.searchParams.set('ticket', ticket)

  return parsed.toString()
}

/** Resolve the MacBook-local profile config, never the connected gateway config. */
export function resolveLocalProfileConfigPath(hermesHome: string, profile: string) {
  const root = path.resolve(hermesHome)
  const alreadyScoped = path.basename(path.dirname(root)) === 'profiles'

  if (alreadyScoped) {
    const defaultRoot = path.dirname(path.dirname(root))

    return path.join(profile === 'default' ? defaultRoot : path.join(defaultRoot, 'profiles', profile), 'config.yaml')
  }

  return path.join(profile === 'default' ? root : path.join(root, 'profiles', profile), 'config.yaml')
}

const PROFILE_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/

/** Mirror Desktop/Python profile precedence for the local browser authority. */
export function resolveDesktopBrowserProfile(
  hermesHome: string,
  explicitProfile: string | null | undefined,
  desktopProfile: string | null | undefined,
  readFile: (file: string) => string
) {
  const valid = (value: unknown) => {
    const text = String(value || '').trim()

    return text && (text === 'default' || PROFILE_RE.test(text)) ? text : null
  }

  const explicit = valid(explicitProfile)

  if (explicit) {return explicit}
  const desktop = valid(desktopProfile)

  if (desktop) {return desktop}
  const root = path.resolve(hermesHome)

  if (path.basename(path.dirname(root)) === 'profiles') {
    const scoped = valid(path.basename(root))

    if (scoped) {return scoped}
  }

  try {
    const sticky = valid(readFile(path.join(root, 'active_profile')))

    if (sticky) {return sticky}
  } catch {
    // Missing/malformed sticky profile is the normal default-profile case.
  }

  return 'default'
}

function yamlLineWithoutComment(line: string) {
  let single = false
  let double = false

  for (let index = 0; index < line.length; index += 1) {
    const char = line[index]

    if (char === "'" && !double) {single = !single}
    else if (char === '"' && !single && line[index - 1] !== '\\') {double = !double}
    else if (char === '#' && !single && !double) {return line.slice(0, index)}
  }

  return line
}

/** Fail-closed narrow reader for the standard block YAML emitted by Hermes. */
export function parseInAppEnabled(yamlText: string) {
  let browserIndent: number | null = null
  let inAppIndent: number | null = null
  let directChildIndent: number | null = null
  let sawBrowser = false
  let sawInApp = false
  let parsedEnabled: boolean | null = null

  for (const rawLine of String(yamlText || '').split(/\r?\n/)) {
    const line = yamlLineWithoutComment(rawLine).replace(/\s+$/, '')

    if (!line.trim()) {continue}
    const leading = line.slice(0, line.length - line.trimStart().length)

    if (/\t/.test(leading)) {return false}
    const indent = leading.length
    const text = line.trim()

    // Only a root-level browser mapping is authoritative. A lookalike nested
    // under another key must never enable a security-sensitive host flag.
    if (indent === 0) {
      browserIndent = null
      inAppIndent = null
      directChildIndent = null

      if (/^browser\s*:/.test(text)) {
        if (text !== 'browser:') {return false}

        if (sawBrowser) {return false}
        sawBrowser = true
        browserIndent = 0
      }

      continue
    }

    if (browserIndent === null || indent <= browserIndent) {continue}

    if (inAppIndent === null) {
      if (/^in_app\s*:/.test(text)) {
        if (text !== 'in_app:') {return false}

        if (sawInApp) {return false}
        sawInApp = true
        inAppIndent = indent
      }

      continue
    }

    if (indent <= inAppIndent) {
      inAppIndent = null
      directChildIndent = null

      continue
    }

    if (directChildIndent === null) {directChildIndent = indent}

    if (indent !== directChildIndent) {continue}
    const match = /^enabled:\s*(true|false)$/i.exec(text)

    if (!match) {
      if (/^enabled\s*:/.test(text)) {return false}

      continue
    }

    if (parsedEnabled !== null) {return false}
    parsedEnabled = match[1].toLowerCase() === 'true'
  }

  return parsedEnabled === true
}

export function readLocalInAppEnabled(
  hermesHome: string,
  profile: string,
  readFile: (file: string) => string
) {
  try {
    return parseInAppEnabled(readFile(resolveLocalProfileConfigPath(hermesHome, profile)))
  } catch {
    return false
  }
}

type DarkState = 'absent' | 'authenticating' | 'negotiating' | 'ready' | 'disabled' | 'incompatible' | 'disconnected'

interface BrowserDarkClientDeps {
  association: DesktopConnectionAssociation
  mintTicket: (connection: any, profile: string, connectionId: string) => Promise<string>
  createWebSocket?: (url: string) => WebSocket
  setInterval?: typeof globalThis.setInterval
  clearInterval?: typeof globalThis.clearInterval
  localEnabled: (profile: string) => boolean
  dispatchCdp?: (request: BrowserCdpAuthenticatedDispatch) => Promise<Record<string, unknown>>
  onLifecycleInvalidated?: () => void
  log?: (message: string) => void
}

export interface BrowserCdpDispatch {
  frame: Record<string, unknown>
  guestGeneration: string
  role: 'automation' | 'raw-cdp'
  tabId: string
  taskGeneration: number
  taskId: string
}

interface BrowserCdpAuthenticatedDispatch extends BrowserCdpDispatch {
  bindingGeneration: number
  capabilityGeneration: number
  connectionId: string
  operationId: string
  profile: string
  remainingDurationMs?: number
}

export interface BrowserTaskBinding {
  guestGeneration: string
  profile: string
  tabId: string
  taskGeneration: number
  taskId: string
}

interface BrowserCdpRoute extends Omit<BrowserCdpAuthenticatedDispatch, 'operationId'> {
  operationId: string | null
  relayToken: string
  sid: string
}

interface ConnectAttempt {
  generation: number
  connection: any
  baseUrl: string
  profile: string
  connectionId: string
}

/** Electron-main-only dark client. It owns no webview or CDP operation. */
export class BrowserDarkClient {
  private readonly deps: Required<BrowserDarkClientDeps>
  private socket: WebSocket | null = null
  private pollTimer: ReturnType<typeof setInterval> | null = null
  private currentProfile: string | null = null
  private currentConnection: any = null
  private awaitingLocalEnable = false
  private readonly cdpRoutes = new Map<string, Omit<BrowserCdpRoute, 'frame' | 'operationId'>>()
  private readonly cdpOperations = new Set<string>()
  private readonly retiredRelayTokens = new Set<string>()
  private readonly taskBindings = new Map<string, BrowserTaskBinding>()
  private readonly latestTaskGenerations = new Map<string, number>()
  private connectGeneration = 0
  private currentStatus = {
    state: 'absent' as DarkState,
    profile: null as string | null,
    sid: null as string | null,
    transportId: null as string | null,
    capabilityGeneration: null as number | null,
    bindingGeneration: null as number | null,
    outcome: null as string | null
  }

  constructor(deps: BrowserDarkClientDeps) {
    this.deps = {
      ...deps,
      createWebSocket: deps.createWebSocket || (url => new WebSocket(url)),
      setInterval: deps.setInterval || globalThis.setInterval,
      clearInterval: deps.clearInterval || globalThis.clearInterval,
      dispatchCdp: deps.dispatchCdp || (async () => {
        throw new Error('browser debugger dispatcher unavailable')
      }),
      onLifecycleInvalidated: deps.onLifecycleInvalidated || (() => undefined),
      log: deps.log || (() => undefined)
    }
  }

  get association() {
    return this.deps.association
  }

  get status() {
    return { ...this.currentStatus }
  }

  private taskBindingKey(profile: string, taskId: string) {
    return `${profile.trim().toLowerCase()}:${taskId}`
  }

  private cdpRouteKey(profile: string, taskId: string, role: BrowserCdpDispatch['role']) {
    return `${profile.trim().toLowerCase()}:${taskId}:${role}`
  }

  private retireProfileBindings(profile: string) {
    const normalized = profile.trim().toLowerCase()

    for (const [key, binding] of this.taskBindings) {
      if (binding.profile === normalized) {this.taskBindings.delete(key)}
    }

    for (const [key, route] of this.cdpRoutes) {
      if (route.profile === normalized) {this.cdpRoutes.delete(key)}
    }
  }

  bindTask(binding: BrowserTaskBinding) {
    const profile = binding.profile.trim().toLowerCase()

    if (!profile) {return false}

    const scopedBinding = { ...binding, profile }
    const key = this.taskBindingKey(profile, binding.taskId)
    const current = this.taskBindings.get(key)
    const latest = this.latestTaskGenerations.get(key)

    if (!current && latest !== undefined && binding.taskGeneration <= latest) {return false}

    if (current && binding.taskGeneration < current.taskGeneration) {return false}

    if (
      current &&
      binding.taskGeneration === current.taskGeneration &&
      (binding.tabId !== current.tabId || binding.guestGeneration !== current.guestGeneration)
    ) {
      return false
    }

    this.taskBindings.set(key, scopedBinding)
    this.latestTaskGenerations.set(key, binding.taskGeneration)
    this.sendTaskLifecycle('bind', scopedBinding)

    return true
  }

  unbindTask(binding: BrowserTaskBinding) {
    const profile = binding.profile.trim().toLowerCase()
    const key = this.taskBindingKey(profile, binding.taskId)
    const current = this.taskBindings.get(key)

    const exact =
      current &&
      current.tabId === binding.tabId &&
      current.guestGeneration === binding.guestGeneration &&
      current.taskGeneration === binding.taskGeneration

    if (!exact) {return false}
    this.taskBindings.delete(key)
    this.cdpRoutes.delete(this.cdpRouteKey(profile, binding.taskId, 'automation'))
    this.cdpRoutes.delete(this.cdpRouteKey(profile, binding.taskId, 'raw-cdp'))
    this.sendTaskLifecycle('unbind', { ...binding, profile })

    return true
  }

  private sendTaskLifecycle(type: 'bind' | 'unbind', binding: BrowserTaskBinding) {
    if (
      !this.socket ||
      this.socket.readyState !== WebSocket.OPEN ||
      this.currentStatus.state !== 'ready' ||
      binding.profile !== this.currentProfile?.trim().toLowerCase()
    ) {return}

    this.socket.send(
      JSON.stringify({
        type: `client.task.${type}`,
        task_id: binding.taskId,
        tab_id: binding.tabId,
        guest_generation: binding.guestGeneration,
        task_generation: binding.taskGeneration
      })
    )
  }

  private isCurrentAttempt(attempt: ConnectAttempt) {
    return (
      attempt.generation === this.connectGeneration &&
      attempt.connection === this.currentConnection &&
      attempt.baseUrl === this.currentConnection?.baseUrl &&
      attempt.profile === this.currentProfile &&
      attempt.connectionId === this.deps.association.connectionId &&
      this.deps.localEnabled(attempt.profile)
    )
  }

  private markAttemptUnavailable(attempt: ConnectAttempt) {
    if (!this.isCurrentAttempt(attempt)) {return}
    this.currentStatus.state = 'disconnected'
    this.currentStatus.outcome = 'browser_unavailable'
    this.stopPoll()
  }

  async connect(connection: any, profile: string) {
    const previousProfile = this.currentProfile?.trim().toLowerCase()
    const nextProfile = profile.trim().toLowerCase()

    if (previousProfile && previousProfile !== nextProfile) {
      this.retireProfileBindings(previousProfile)
    }

    this.disconnect()
    this.currentConnection = connection
    this.currentProfile = nextProfile

    const attempt: ConnectAttempt = {
      generation: ++this.connectGeneration,
      connection,
      baseUrl: connection?.baseUrl,
      profile: nextProfile,
      connectionId: this.deps.association.connectionId
    }

    this.currentStatus = {
      state: 'authenticating',
      profile: nextProfile,
      sid: null,
      transportId: null,
      capabilityGeneration: null,
      bindingGeneration: null,
      outcome: null
    }

    if (!this.deps.localEnabled(nextProfile)) {
      this.currentStatus.state = 'disabled'
      this.currentStatus.outcome = 'browser_disabled'
      this.awaitingLocalEnable = true
      this.armLocalFlagPoll()

      return
    }

    // Poll while the one-shot ticket request is in flight too. A local
    // disable must invalidate the attempt before its delayed mint can return.
    this.armLocalFlagPoll()
    let ticket: string

    try {
      ticket = await this.deps.mintTicket(connection, nextProfile, this.deps.association.connectionId)
    } catch (error) {
      this.markAttemptUnavailable(attempt)
      throw error
    }

    if (!this.isCurrentAttempt(attempt)) {return}

    let socket: WebSocket

    try {
      socket = this.deps.createWebSocket(buildBrowserWsUrl(attempt.baseUrl, ticket))
    } catch (error) {
      this.markAttemptUnavailable(attempt)
      throw error
    }

    if (!this.isCurrentAttempt(attempt)) {
      socket.close(1000, 'stale browser connect attempt')

      return
    }

    this.socket = socket
    socket.addEventListener('open', () => {
      if (this.socket !== socket || !this.isCurrentAttempt(attempt)) {
        if (this.socket === socket) {this.disableLocal()}

        return
      }

      this.currentStatus.state = 'negotiating'
      socket.send(
        JSON.stringify({
          type: 'client.hello',
          profile: nextProfile,
          connection_id: this.deps.association.connectionId,
          browser: {
            present: true,
            local_enabled: true,
            protocol: WIRE_CONTRACT.protocol,
            method_set_hash: methodSetHash(),
            methods: WIRE_CONTRACT.required_methods.map(row => row.name)
          }
        })
      )
    })
    socket.addEventListener('message', event => {
      if (this.socket !== socket || !this.isCurrentAttempt(attempt)) {return}

      try {
        const message = JSON.parse(String(event.data))

        if (message?.type === 'browser.cdp.send') {
          void this.onCdpSend(message).catch(() => {
            socket.close(4400, 'invalid browser operational frame')
          })
        } else if (!this.onServerMessage(message)) {throw new Error('invalid browser protocol frame')}
      } catch {
        this.currentStatus.state = 'incompatible'
        this.currentStatus.outcome = 'browser_incompatible'
        socket.close(4400, 'invalid browser protocol frame')
      }
    })
    socket.addEventListener('close', () => {
      if (this.socket !== socket || attempt.generation !== this.connectGeneration) {return}
      this.socket = null
      this.cdpRoutes.clear()
      this.cdpOperations.clear()
      this.retiredRelayTokens.clear()
      this.deps.onLifecycleInvalidated()

      if (!['disabled', 'incompatible'].includes(this.currentStatus.state)) {
        this.currentStatus.state = 'disconnected'
        this.currentStatus.outcome ||= 'browser_disconnected'
      }
    })
    this.armLocalFlagPoll()
  }

  async revalidate() {
    // A socket that survived sleep may still report OPEN after its TCP path is
    // dead. Replace only ready/disconnected transient states; an in-flight
    // dial is already the single owner, while disabled/incompatible states
    // require a configuration or version change rather than a hot retry.
    if (this.currentStatus.state !== 'ready' && this.currentStatus.state !== 'disconnected') {
      return false
    }

    const connection = this.currentConnection
    const profile = this.currentProfile

    if (!connection || !profile || !this.deps.localEnabled(profile)) {return false}

    // connect() synchronously enters authenticating before its first await and
    // bumps the generation, so concurrent wake/unlock signals collapse to one
    // fresh ticket/socket and every completion from the old socket is fenced.
    await this.connect(connection, profile)

    return true
  }

  private onServerMessage(message: any): boolean {
    if (message?.type === 'server.task.bound' || message?.type === 'server.task.unbound') {
      return (
        typeof message.task_id === 'string' &&
        typeof message.tab_id === 'string' &&
        typeof message.guest_generation === 'string' &&
        Number.isSafeInteger(message.task_generation)
      )
    }

    if (typeof message?.status !== 'string') {return false}
    const status = message.status

    if (status === 'browser_task_already_bound') {
      // A narrow predecessor/successor registration race is task-scoped. The
      // gateway normally preempts the older exact task before this fallback,
      // but accepting the typed outcome keeps unrelated sibling relays alive.
      return message.delivery === 'not_started' && message.retryable === true
    }

    if (status === 'ready') {
      const capabilityGeneration = message.capability_generation
      const bindingGeneration = message.binding_generation
      const sid = message.sid
      const transportId = message.transport_id
      const protocol = message.protocol

      const compatible =
        message?.type === 'server.hello' &&
        typeof sid === 'string' &&
        sid.length > 0 &&
        typeof transportId === 'string' &&
        transportId.length > 0 &&
        transportId !== sid &&
        typeof capabilityGeneration === 'number' &&
        Number.isSafeInteger(capabilityGeneration) &&
        capabilityGeneration > 0 &&
        typeof bindingGeneration === 'number' &&
        Number.isSafeInteger(bindingGeneration) &&
        bindingGeneration > 0 &&
        protocol !== null &&
        typeof protocol === 'object' &&
        !Array.isArray(protocol) &&
        typeof protocol.major === 'number' &&
        Number.isSafeInteger(protocol.major) &&
        protocol.major === WIRE_CONTRACT.protocol.major &&
        typeof protocol.minor === 'number' &&
        Number.isSafeInteger(protocol.minor) &&
        protocol.minor === WIRE_CONTRACT.protocol.minor &&
        typeof message.method_set_hash === 'string' &&
        message.method_set_hash === methodSetHash()

      if (!compatible) {return false}

      // A successful hello establishes a new browser capability incarnation.
      // No route or operation identity from an earlier incarnation may cross
      // into it, even when the Desktop window and tabs survive.
      this.cdpRoutes.clear()
      this.cdpOperations.clear()
      this.retiredRelayTokens.clear()

      this.currentStatus = {
        state: 'ready',
        profile: this.currentProfile,
        sid,
        transportId,
        capabilityGeneration,
        bindingGeneration,
        outcome: null
      }

      for (const binding of this.taskBindings.values()) {
        if (binding.profile === this.currentProfile) {this.sendTaskLifecycle('bind', binding)}
      }

      return true
    }

    if (status === 'browser_incompatible') {this.currentStatus.state = 'incompatible'}
    else if (status === 'browser_disabled' || status === 'browser_killed' || status === 'browser_revoked') {
      this.currentStatus.state = 'disabled'
    } else {return false}

    this.currentStatus.outcome = status

    return true
  }

  private parseCdpRoute(message: any): BrowserCdpRoute | null {
    const frame = message?.frame

    const route: BrowserCdpRoute = {
      bindingGeneration: message?.binding_generation,
      capabilityGeneration: message?.capability_generation,
      connectionId: this.deps.association.connectionId,
      frame,
      guestGeneration: message?.guest_generation,
      operationId: message?.operation_id,
      profile: message?.profile,
      relayToken: message?.relay_token,
      role: message?.role,
      sid: message?.sid,
      tabId: message?.tab_id,
      taskGeneration: message?.task_generation,
      taskId: message?.task_id
    }

    const positiveInteger = (value: unknown) =>
      typeof value === 'number' && Number.isSafeInteger(value) && value > 0

    return (
      this.currentStatus.state === 'ready' &&
      route.profile === this.currentStatus.profile &&
      route.sid === this.currentStatus.sid &&
      route.capabilityGeneration === this.currentStatus.capabilityGeneration &&
      route.bindingGeneration === this.currentStatus.bindingGeneration &&
      typeof route.relayToken === 'string' &&
      route.relayToken.length >= 43 &&
      typeof route.taskId === 'string' &&
      route.taskId.length > 0 &&
      typeof route.tabId === 'string' &&
      route.tabId.length > 0 &&
      typeof route.guestGeneration === 'string' &&
      route.guestGeneration.length > 0 &&
      (route.role === 'automation' || route.role === 'raw-cdp') &&
      positiveInteger(route.taskGeneration) &&
      ((typeof route.operationId === 'string' && route.operationId.length > 0) ||
        (route.operationId === null && frame?.type === 'browser.relay.close')) &&
      frame !== null &&
      typeof frame === 'object' &&
      !Array.isArray(frame)
    )
      ? route
      : null
  }

  private isCurrentCdpRoute(route: BrowserCdpRoute, socket: WebSocket, connectGeneration: number) {
    return (
      this.socket === socket &&
      socket.readyState === WebSocket.OPEN &&
      this.connectGeneration === connectGeneration &&
      this.currentStatus.state === 'ready' &&
      route.profile === this.currentStatus.profile &&
      route.sid === this.currentStatus.sid &&
      route.capabilityGeneration === this.currentStatus.capabilityGeneration &&
      route.bindingGeneration === this.currentStatus.bindingGeneration &&
      !this.retiredRelayTokens.has(route.relayToken)
    )
  }

  private sendCdpFrame(
    route: BrowserCdpRoute,
    frame: Record<string, unknown>,
    operationId: string | null,
    socket: WebSocket = this.socket as WebSocket,
    connectGeneration = this.connectGeneration
  ) {
    if (!socket || !this.isCurrentCdpRoute(route, socket, connectGeneration)) {return false}
    socket.send(
      JSON.stringify({
        type: 'browser.cdp.frame',
        sid: route.sid,
        profile: route.profile,
        capability_generation: route.capabilityGeneration,
        binding_generation: route.bindingGeneration,
        relay_token: route.relayToken,
        task_id: route.taskId,
        tab_id: route.tabId,
        guest_generation: route.guestGeneration,
        role: route.role,
        task_generation: route.taskGeneration,
        operation_id: operationId,
        frame
      })
    )

    return true
  }

  private async onCdpSend(message: any) {
    const route = this.parseCdpRoute(message)

    if (!route) {throw new Error('browser operational fence mismatch')}

    if (route.operationId === null) {
      const socket = this.socket
      const connectGeneration = this.connectGeneration

      if (!socket || !this.isCurrentCdpRoute(route, socket, connectGeneration)) {return}

      const key = this.cdpRouteKey(route.profile, route.taskId, route.role)
      const current = this.cdpRoutes.get(key)

      this.retiredRelayTokens.add(route.relayToken)

      if (current?.relayToken === route.relayToken) {this.cdpRoutes.delete(key)}

      return
    }

    // Record before the first await so duplicate delivery cannot execute CDP
    // twice. Operation identities are retired only with the capability.
    if (this.cdpOperations.has(route.operationId!)) {return}
    this.cdpOperations.add(route.operationId!)
    const socket = this.socket
    const connectGeneration = this.connectGeneration

    if (!socket || !this.isCurrentCdpRoute(route, socket, connectGeneration)) {return}

    const response = await this.deps.dispatchCdp({
      bindingGeneration: route.bindingGeneration,
      capabilityGeneration: route.capabilityGeneration,
      connectionId: route.connectionId,
      frame: route.frame,
      guestGeneration: route.guestGeneration,
      operationId: route.operationId,
      profile: route.profile,
      remainingDurationMs:
        Number.isSafeInteger(message.remaining_duration_ms) &&
        message.remaining_duration_ms >= 0 &&
        message.remaining_duration_ms <= 120_000
          ? message.remaining_duration_ms
          : undefined,
      role: route.role,
      tabId: route.tabId,
      taskGeneration: route.taskGeneration,
      taskId: route.taskId
    })

    // A debugger call may outlive disconnect, re-hello, profile switch, or
    // generation retirement. Never route that result through a successor.
    if (!this.isCurrentCdpRoute(route, socket, connectGeneration)) {return}

    const error = response.error as { data?: { hermesCode?: unknown } } | undefined

    if (error?.data?.hermesCode !== 'NAVIGATION_TARGET_STALE') {
      const { frame: _frame, operationId: _operationId, ...remembered } = route

      this.cdpRoutes.set(this.cdpRouteKey(route.profile, route.taskId, route.role), remembered)
    }

    this.sendCdpFrame(route, response, route.operationId, socket, connectGeneration)
  }

  forwardCdpEvent(event: BrowserCdpDispatch) {
    const profile = this.currentStatus.profile

    if (!profile) {return false}

    const route = this.cdpRoutes.get(this.cdpRouteKey(profile, event.taskId, event.role))

    if (
      !route ||
      route.tabId !== event.tabId ||
      route.guestGeneration !== event.guestGeneration ||
      route.role !== event.role ||
      route.taskGeneration !== event.taskGeneration
    ) {
      return false
    }

    this.sendCdpFrame({ ...route, frame: event.frame, operationId: null }, event.frame, null)

    return true
  }

  private disableLocal() {
    this.connectGeneration += 1
    this.cdpRoutes.clear()
    this.cdpOperations.clear()
    this.retiredRelayTokens.clear()
    this.currentStatus.state = 'disabled'
    this.currentStatus.outcome = 'browser_disabled'
    const socket = this.socket

    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: 'client.disable' }))
    }

    socket?.close(4410, 'browser_disabled')
    this.socket = null
    this.awaitingLocalEnable = true
    this.stopPoll()
    this.armLocalFlagPoll()
  }

  private armLocalFlagPoll() {
    this.stopPoll()
    this.pollTimer = this.deps.setInterval(() => {
      if (!this.currentProfile) {return}
      const enabled = this.deps.localEnabled(this.currentProfile)

      if (!enabled && !this.awaitingLocalEnable) {
        this.disableLocal()

        return
      }

      if (enabled && this.awaitingLocalEnable && this.currentConnection) {
        const connection = this.currentConnection
        const profile = this.currentProfile
        this.awaitingLocalEnable = false
        void this.connect(connection, profile).catch(() => {
          // Ticket/auth failures are surfaced by the owning main process; no
          // hot retry loop or cached-ticket fallback is permitted here.
        })
      }
    }, 250)
  }

  private stopPoll() {
    if (this.pollTimer !== null) {this.deps.clearInterval(this.pollTimer)}
    this.pollTimer = null
  }

  disconnect() {
    this.connectGeneration += 1
    this.cdpRoutes.clear()
    this.cdpOperations.clear()
    this.retiredRelayTokens.clear()
    this.deps.onLifecycleInvalidated()
    this.stopPoll()
    const socket = this.socket
    this.socket = null
    socket?.close(1000, 'browser client disconnect')
    this.currentConnection = null
    this.currentProfile = null
    this.awaitingLocalEnable = false

    if (this.currentStatus.state !== 'absent') {this.currentStatus.state = 'disconnected'}
  }
}
