import assert from 'node:assert/strict'
import crypto from 'node:crypto'

import { test } from 'vitest'

import {
  appendDesktopAssociation,
  BrowserDarkClient,
  buildBrowserWsUrl,
  DesktopConnectionAssociation,
  methodSetHash,
  parseInAppEnabled,
  readLocalInAppEnabled,
  resolveDesktopBrowserProfile,
  resolveLocalProfileConfigPath,
  WIRE_CONTRACT
} from './browser-dark-client'

class FakeWebSocket {
  static OPEN = 1
  readyState = 0
  sent: string[] = []
  closeCalls: Array<[number | undefined, string | undefined]> = []
  listeners = new Map<string, Array<(event: any) => void>>()

  addEventListener(type: string, listener: (event: any) => void) {
    const rows = this.listeners.get(type) || []
    rows.push(listener)
    this.listeners.set(type, rows)
  }

  send(value: string) {
    this.sent.push(value)
  }

  close(code?: number, reason?: string) {
    this.closeCalls.push([code, reason])
    this.readyState = 3
  }

  open() {
    this.readyState = 1
    this.emit('open', {})
  }

  message(payload: object) {
    this.emit('message', { data: JSON.stringify(payload) })
  }

  serverClose() {
    this.readyState = 3
    this.emit('close', {})
  }

  private emit(type: string, event: any) {
    for (const listener of this.listeners.get(type) || []) {listener(event)}
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void

  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve
    reject = onReject
  })

  return { promise, reject, resolve }
}

test('association is 256-bit, unique, and has no persistence surface', () => {
  const values = new Set(Array.from({ length: 256 }, () => new DesktopConnectionAssociation().connectionId))
  assert.equal(values.size, 256)

  for (const value of values) {
    assert.equal(Buffer.from(value, 'base64url').byteLength, 32)
  }

  const association = new DesktopConnectionAssociation()
  assert.equal(Object.hasOwn(association, 'path'), false)
  assert.equal('save' in association, false)
  assert.notEqual(new DesktopConnectionAssociation().connectionId, association.connectionId)
})

test('chat URL association carries no new bearer and browser URL remains ticket-only', () => {
  const association = new DesktopConnectionAssociation(() => Buffer.alloc(32, 7))
  const url = appendDesktopAssociation('wss://host/api/ws?ticket=fresh', association.connectionId, 'gpt')
  const parsed = new URL(url)
  assert.equal(parsed.searchParams.get('ticket'), 'fresh')
  assert.equal(parsed.searchParams.get('connection_id'), association.connectionId)
  assert.equal(parsed.searchParams.get('profile'), 'gpt')
  assert.equal(parsed.searchParams.has('token'), false)
  assert.equal(
    buildBrowserWsUrl('https://host/prefix/', 'one-shot'),
    'wss://host/prefix/api/ws/browser?ticket=one-shot'
  )
})

test('local flag resolves independently for default, named, and already-profile-scoped homes', () => {
  assert.equal(resolveLocalProfileConfigPath('/home/k/.hermes', 'default'), '/home/k/.hermes/config.yaml')
  assert.equal(resolveLocalProfileConfigPath('/home/k/.hermes', 'gpt'), '/home/k/.hermes/profiles/gpt/config.yaml')
  assert.equal(
    resolveLocalProfileConfigPath('/home/k/.hermes/profiles/gpt', 'gpt'),
    '/home/k/.hermes/profiles/gpt/config.yaml'
  )
  assert.equal(
    resolveLocalProfileConfigPath('/home/k/.hermes/profiles/gpt', 'default'),
    '/home/k/.hermes/config.yaml'
  )

  const files = new Map([
    ['/home/k/.hermes/config.yaml', 'browser:\n  in_app:\n    enabled: false\n'],
    ['/home/k/.hermes/profiles/gpt/config.yaml', 'browser:\n  in_app:\n    enabled: true # local MacBook only\n']
  ])

  const read = (file: string) => {
    const value = files.get(file)

    if (value === undefined) {throw new Error('missing')}

    return value
  }

  assert.equal(readLocalInAppEnabled('/home/k/.hermes', 'default', read), false)
  assert.equal(readLocalInAppEnabled('/home/k/.hermes', 'gpt', read), true)
  assert.equal(readLocalInAppEnabled('/missing', 'gpt', read), false)
})

test('local flag parser rejects nested lookalikes, duplicate authority, and indirect enabled keys', () => {
  assert.equal(parseInAppEnabled('wrapper:\n  browser:\n    in_app:\n      enabled: true\n'), false)
  assert.equal(
    parseInAppEnabled('browser:\n  in_app:\n    enabled: true\nbrowser:\n  in_app:\n    enabled: false\n'),
    false
  )
  assert.equal(parseInAppEnabled('browser:\n  in_app:\n    nested:\n      enabled: true\n'), false)
  assert.equal(parseInAppEnabled('browser: {}\nbrowser:\n  in_app:\n    enabled: true\n'), false)
  assert.equal(parseInAppEnabled('browser:\n  in_app:\n    enabled: invalid\n    enabled: true\n'), false)
  assert.equal(parseInAppEnabled('browser:\n  in_app:\n    enabled: true\n'), true)
})

test('Desktop browser profile mirrors explicit, Desktop, scoped-home, then sticky precedence', () => {
  const readSticky = (file: string) => {
    assert.equal(file, '/home/k/.hermes/active_profile')

    return 'gpt\n'
  }

  assert.equal(resolveDesktopBrowserProfile('/home/k/.hermes', 'writer', 'gpt', readSticky), 'writer')
  assert.equal(resolveDesktopBrowserProfile('/home/k/.hermes', null, 'writer', readSticky), 'writer')
  assert.equal(resolveDesktopBrowserProfile('/home/k/.hermes/profiles/gpt', null, null, readSticky), 'gpt')
  assert.equal(resolveDesktopBrowserProfile('/home/k/.hermes', null, null, readSticky), 'gpt')
  assert.equal(
    resolveDesktopBrowserProfile('/home/k/.hermes', null, null, () => {
      throw new Error('missing')
    }),
    'default'
  )
})

test('method-set hash derives from the canonical contract', () => {
  assert.equal(methodSetHash(), methodSetHash([...WIRE_CONTRACT.required_methods].reverse()))
  const changed = WIRE_CONTRACT.required_methods.map(row => ({ ...row }))
  changed[0].direction = 'wrong'
  assert.notEqual(methodSetHash(changed), methodSetHash())
})

test('dark client mints a fresh ticket for each dial and sends compatible hello only after open', async () => {
  const sockets: FakeWebSocket[] = []
  const minted: string[] = []
  const association = new DesktopConnectionAssociation(() => Buffer.alloc(32, 3))

  const client = new BrowserDarkClient({
    association,
    mintTicket: async () => {
      const ticket = `fresh-${minted.length}`
      minted.push(ticket)

      return ticket
    },
    createWebSocket: url => {
      assert.match(url, /\/api\/ws\/browser\?ticket=fresh-/)
      assert.equal(new URL(url).searchParams.size, 1)
      const socket = new FakeWebSocket()
      sockets.push(socket)

      return socket as any
    },
    setInterval: (() => 1) as any,
    clearInterval: (() => undefined) as any,
    localEnabled: () => true
  })

  await client.connect({ baseUrl: 'https://host', authMode: 'oauth' }, 'gpt')
  assert.equal(minted.length, 1)
  assert.equal(sockets[0].sent.length, 0)
  sockets[0].open()
  const hello = JSON.parse(sockets[0].sent[0])
  assert.equal(hello.type, 'client.hello')
  assert.equal(hello.connection_id, association.connectionId)
  assert.equal(hello.browser.method_set_hash, methodSetHash())

  client.disconnect()
  await client.connect({ baseUrl: 'https://host', authMode: 'oauth' }, 'gpt')
  assert.equal(minted.length, 2)
  assert.notEqual(minted[0], minted[1])
})

test('ticket and socket startup failures become explicitly retryable disconnected attempts', async () => {
  const sockets: FakeWebSocket[] = []
  let mintCount = 0
  let socketCount = 0

  const client = new BrowserDarkClient({
    association: new DesktopConnectionAssociation(() => Buffer.alloc(32, 23)),
    mintTicket: async () => {
      mintCount += 1

      if (mintCount === 1) {throw new Error('gateway unavailable')}

      return `ticket-${mintCount}`
    },
    createWebSocket: () => {
      socketCount += 1

      if (socketCount === 1) {throw new Error('socket unavailable')}
      const socket = new FakeWebSocket()
      sockets.push(socket)

      return socket as any
    },
    setInterval: (() => 1) as any,
    clearInterval: (() => undefined) as any,
    localEnabled: () => true
  })

  await assert.rejects(client.connect({ baseUrl: 'https://host' }, 'gpt'), /gateway unavailable/)
  assert.equal(client.status.state, 'disconnected')
  assert.equal(client.status.outcome, 'browser_unavailable')

  await assert.rejects(client.revalidate(), /socket unavailable/)
  assert.equal(client.status.state, 'disconnected')
  assert.equal(client.status.outcome, 'browser_unavailable')

  assert.equal(await client.revalidate(), true)
  assert.equal(mintCount, 3)
  assert.equal(socketCount, 2)
  assert.equal(sockets.length, 1)
  assert.equal(client.status.state, 'authenticating')
})

test('wake revalidation replaces a ready zombie with one fresh authenticated dial', async () => {
  const sockets: FakeWebSocket[] = []
  let mintCount = 0

  const client = new BrowserDarkClient({
    association: new DesktopConnectionAssociation(() => Buffer.alloc(32, 21)),
    mintTicket: async () => `wake-ticket-${++mintCount}`,
    createWebSocket: () => {
      const socket = new FakeWebSocket()
      sockets.push(socket)

      return socket as any
    },
    setInterval: (() => 1) as any,
    clearInterval: (() => undefined) as any,
    localEnabled: () => true
  })

  await client.connect({ baseUrl: 'https://host', authMode: 'oauth' }, 'gpt')
  sockets[0].open()
  sockets[0].message({
    type: 'server.hello',
    status: 'ready',
    transport_id: 'transport-server',
    protocol: WIRE_CONTRACT.protocol,
    method_set_hash: methodSetHash(),
    capability_generation: 1,
    binding_generation: 1,
    sid: 'sid-before-sleep'
  })

  const firstWake = client.revalidate()
  const duplicateWake = client.revalidate()
  assert.equal(await duplicateWake, false)
  assert.equal(await firstWake, true)
  assert.equal(mintCount, 2)
  assert.equal(sockets.length, 2)
  assert.deepEqual(sockets[0].closeCalls.at(-1), [1000, 'browser client disconnect'])
  assert.equal(sockets[1].sent.length, 0)

  sockets[1].open()
  assert.equal(JSON.parse(sockets[1].sent[0]).type, 'client.hello')
  assert.equal(client.status.state, 'negotiating')
})

test('wake revalidation redials a disconnected socket but not terminal or unowned states', async () => {
  const sockets: FakeWebSocket[] = []
  let enabled = true
  let mintCount = 0

  const client = new BrowserDarkClient({
    association: new DesktopConnectionAssociation(() => Buffer.alloc(32, 22)),
    mintTicket: async () => `ticket-${++mintCount}`,
    createWebSocket: () => {
      const socket = new FakeWebSocket()
      sockets.push(socket)

      return socket as any
    },
    setInterval: (() => 1) as any,
    clearInterval: (() => undefined) as any,
    localEnabled: () => enabled
  })

  assert.equal(await client.revalidate(), false)
  await client.connect({ baseUrl: 'https://host' }, 'gpt')
  sockets[0].open()
  sockets[0].serverClose()
  assert.equal(client.status.state, 'disconnected')
  assert.equal(await client.revalidate(), true)
  assert.equal(mintCount, 2)
  assert.equal(sockets.length, 2)

  sockets[1].open()
  sockets[1].message({
    type: 'server.hello',
    status: 'ready',
    transport_id: 'transport-server',
    protocol: WIRE_CONTRACT.protocol,
    method_set_hash: '0'.repeat(64),
    capability_generation: 2,
    binding_generation: 2,
    sid: 'incompatible'
  })
  assert.equal(client.status.state, 'incompatible')
  assert.equal(await client.revalidate(), false)

  enabled = false
  assert.equal(await client.revalidate(), false)
  assert.equal(mintCount, 2)
})

test('dark client tracks generations and local disable closes only its browser socket', async () => {
  let enabled = true
  let intervalCallback: (() => void) | undefined
  const socket = new FakeWebSocket()

  const client = new BrowserDarkClient({
    association: new DesktopConnectionAssociation(() => crypto.randomBytes(32)),
    mintTicket: async () => 'ticket',
    createWebSocket: () => socket as any,
    setInterval: ((fn: () => void) => {
      intervalCallback = fn

      return 1
    }) as any,
    clearInterval: (() => undefined) as any,
    localEnabled: () => enabled
  })

  await client.connect({ baseUrl: 'http://127.0.0.1:1', authMode: 'token' }, 'gpt')
  socket.open()
  socket.message({
    type: 'server.hello',
    status: 'ready',
    transport_id: 'transport-server',
    protocol: WIRE_CONTRACT.protocol,
    method_set_hash: methodSetHash(),
    capability_generation: 9,
    binding_generation: 11,
    sid: 'sid-1'
  })
  assert.deepEqual(client.status, {
    state: 'ready',
    profile: 'gpt',
    sid: 'sid-1',
    transportId: 'transport-server',
    capabilityGeneration: 9,
    bindingGeneration: 11,
    outcome: null
  })

  enabled = false
  intervalCallback?.()
  assert.equal(socket.closeCalls.length, 1)
  assert.equal(client.status.state, 'disabled')
  assert.equal(client.status.outcome, 'browser_disabled')
})

test('dark client fails closed on a forged ready response', async () => {
  const socket = new FakeWebSocket()

  const client = new BrowserDarkClient({
    association: new DesktopConnectionAssociation(() => Buffer.alloc(32, 9)),
    mintTicket: async () => 'ticket',
    createWebSocket: () => socket as any,
    setInterval: (() => 1) as any,
    clearInterval: (() => undefined) as any,
    localEnabled: () => true
  })

  await client.connect({ baseUrl: 'https://host', authMode: 'oauth' }, 'gpt')
  socket.open()
  socket.message({
    type: 'server.hello',
    status: 'ready',
    transport_id: 'transport-server',
    protocol: WIRE_CONTRACT.protocol,
    method_set_hash: '0'.repeat(64),
    capability_generation: 1,
    binding_generation: 1,
    sid: 'forged'
  })
  assert.equal(client.status.state, 'incompatible')
  assert.equal(client.status.outcome, 'browser_incompatible')
  assert.deepEqual(socket.closeCalls.at(-1), [4400, 'invalid browser protocol frame'])
})

test.each([
  ['type', 1],
  ['status', 1],
  ['sid', 1],
  ['capability_generation', true],
  ['capability_generation', '1'],
  ['capability_generation', 1.5],
  ['binding_generation', true],
  ['binding_generation', '1'],
  ['binding_generation', 1.5],
  ['protocol', { major: true, minor: 0 }],
  ['protocol', { major: 1, minor: '0' }],
  ['method_set_hash', 1]
] as const)('dark client strictly rejects malformed server hello field %s=%j', async (field, value) => {
  const socket = new FakeWebSocket()

  const client = new BrowserDarkClient({
    association: new DesktopConnectionAssociation(() => Buffer.alloc(32, 15)),
    mintTicket: async () => 'ticket',
    createWebSocket: () => socket as any,
    setInterval: (() => 1) as any,
    clearInterval: (() => undefined) as any,
    localEnabled: () => true
  })

  const hello: Record<string, unknown> = {
    type: 'server.hello',
    status: 'ready',
    transport_id: 'transport-server',
    protocol: WIRE_CONTRACT.protocol,
    method_set_hash: methodSetHash(),
    capability_generation: 1,
    binding_generation: 1,
    sid: 'sid'
  }

  hello[field] = value
  await client.connect({ baseUrl: 'https://host' }, 'gpt')
  socket.open()
  socket.message(hello)

  assert.equal(client.status.state, 'incompatible')
  assert.deepEqual(socket.closeCalls.at(-1), [4400, 'invalid browser protocol frame'])
})

test('local re-enable mints a fresh ticket instead of replaying the disabled dial', async () => {
  let enabled = false
  let intervalCallback: (() => void) | undefined
  let mintCount = 0
  const sockets: FakeWebSocket[] = []

  const client = new BrowserDarkClient({
    association: new DesktopConnectionAssociation(() => Buffer.alloc(32, 8)),
    mintTicket: async () => `ticket-${++mintCount}`,
    createWebSocket: () => {
      const socket = new FakeWebSocket()
      sockets.push(socket)

      return socket as any
    },
    setInterval: ((fn: () => void) => {
      intervalCallback = fn

      return 1
    }) as any,
    clearInterval: (() => undefined) as any,
    localEnabled: () => enabled
  })

  await client.connect({ baseUrl: 'https://host', authMode: 'oauth' }, 'gpt')
  assert.equal(mintCount, 0)
  assert.equal(client.status.state, 'disabled')
  enabled = true
  intervalCallback?.()
  await Promise.resolve()
  await Promise.resolve()
  assert.equal(mintCount, 1)
  assert.equal(sockets.length, 1)
})

test('disconnect invalidates a delayed ticket mint before it can create a socket or hello', async () => {
  const minted = deferred<string>()
  const sockets: FakeWebSocket[] = []

  const client = new BrowserDarkClient({
    association: new DesktopConnectionAssociation(() => Buffer.alloc(32, 10)),
    mintTicket: () => minted.promise,
    createWebSocket: () => {
      const socket = new FakeWebSocket()
      sockets.push(socket)

      return socket as any
    },
    setInterval: (() => 1) as any,
    clearInterval: (() => undefined) as any,
    localEnabled: () => true
  })

  const connecting = client.connect({ baseUrl: 'https://old-host' }, 'gpt')
  await Promise.resolve()
  client.disconnect()
  minted.resolve('stale-ticket')
  await connecting

  assert.equal(sockets.length, 0)
  assert.equal(client.status.state, 'disconnected')
})

test('local disable invalidates a delayed ticket mint before it can create a socket or hello', async () => {
  const minted = deferred<string>()
  const sockets: FakeWebSocket[] = []
  let enabled = true
  let poll: (() => void) | undefined

  const client = new BrowserDarkClient({
    association: new DesktopConnectionAssociation(() => Buffer.alloc(32, 11)),
    mintTicket: () => minted.promise,
    createWebSocket: () => {
      const socket = new FakeWebSocket()
      sockets.push(socket)

      return socket as any
    },
    setInterval: ((callback: () => void) => {
      poll = callback

      return 1
    }) as any,
    clearInterval: (() => undefined) as any,
    localEnabled: () => enabled
  })

  const connecting = client.connect({ baseUrl: 'https://host' }, 'gpt')
  await Promise.resolve()
  enabled = false
  poll?.()
  minted.resolve('disabled-ticket')
  await connecting

  assert.equal(sockets.length, 0)
  assert.equal(client.status.state, 'disabled')
  assert.equal(client.status.outcome, 'browser_disabled')
})

test('profile switch invalidates the older delayed mint and only the selected profile says hello', async () => {
  const oldMint = deferred<string>()
  const sockets: Array<{ socket: FakeWebSocket; url: string }> = []
  let mintCount = 0

  const client = new BrowserDarkClient({
    association: new DesktopConnectionAssociation(() => Buffer.alloc(32, 12)),
    mintTicket: () => (mintCount++ === 0 ? oldMint.promise : Promise.resolve('new-ticket')),
    createWebSocket: url => {
      const socket = new FakeWebSocket()
      sockets.push({ socket, url })

      return socket as any
    },
    setInterval: (() => 1) as any,
    clearInterval: (() => undefined) as any,
    localEnabled: () => true
  })

  const oldConnect = client.connect({ baseUrl: 'https://same-gateway' }, 'default')
  await Promise.resolve()
  await client.connect({ baseUrl: 'https://same-gateway' }, 'gpt')
  oldMint.resolve('old-ticket')
  await oldConnect

  assert.equal(sockets.length, 1)
  assert.equal(new URL(sockets[0].url).searchParams.get('ticket'), 'new-ticket')
  sockets[0].socket.open()
  assert.equal(JSON.parse(sockets[0].socket.sent[0]).profile, 'gpt')
})

test('gateway switch invalidates the older delayed mint and never creates its stale socket', async () => {
  const oldMint = deferred<string>()
  const socketUrls: string[] = []
  let mintCount = 0

  const client = new BrowserDarkClient({
    association: new DesktopConnectionAssociation(() => Buffer.alloc(32, 13)),
    mintTicket: () => (mintCount++ === 0 ? oldMint.promise : Promise.resolve('new-ticket')),
    createWebSocket: url => {
      socketUrls.push(url)

      return new FakeWebSocket() as any
    },
    setInterval: (() => 1) as any,
    clearInterval: (() => undefined) as any,
    localEnabled: () => true
  })

  const oldConnect = client.connect({ baseUrl: 'https://old-gateway' }, 'gpt')
  await Promise.resolve()
  await client.connect({ baseUrl: 'https://new-gateway' }, 'gpt')
  oldMint.resolve('old-ticket')
  await oldConnect

  assert.equal(socketUrls.length, 1)
  assert.equal(new URL(socketUrls[0]).origin, 'wss://new-gateway')
})

test('out-of-order concurrent connects allow only the newest mint to assign a socket or hello', async () => {
  const firstMint = deferred<string>()
  const secondMint = deferred<string>()
  const sockets: FakeWebSocket[] = []
  let mintCount = 0

  const client = new BrowserDarkClient({
    association: new DesktopConnectionAssociation(() => Buffer.alloc(32, 14)),
    mintTicket: () => (mintCount++ === 0 ? firstMint.promise : secondMint.promise),
    createWebSocket: () => {
      const socket = new FakeWebSocket()
      sockets.push(socket)

      return socket as any
    },
    setInterval: (() => 1) as any,
    clearInterval: (() => undefined) as any,
    localEnabled: () => true
  })

  const first = client.connect({ baseUrl: 'https://host' }, 'gpt')
  await Promise.resolve()
  const second = client.connect({ baseUrl: 'https://host' }, 'gpt')
  secondMint.resolve('newest-ticket')
  await second
  firstMint.resolve('oldest-ticket')
  await first

  assert.equal(sockets.length, 1)
  sockets[0].open()
  assert.equal(sockets[0].sent.length, 1)
  assert.equal(client.status.state, 'negotiating')
})

test('dark client dispatches exact operational frames and forwards debugger events on the browser socket', async () => {
  const socket = new FakeWebSocket()
  const dispatchCalls: any[] = []

  const client = new BrowserDarkClient({
    association: new DesktopConnectionAssociation(() => Buffer.alloc(32, 4)),
    mintTicket: async () => 'ticket',
    createWebSocket: () => socket as any,
    setInterval: (() => 1) as any,
    clearInterval: (() => undefined) as any,
    localEnabled: () => true,
    dispatchCdp: async request => {
      dispatchCalls.push(request)

      return { id: request.frame.id, result: { frameId: 'frame-1' } }
    }
  })

  await client.connect({ baseUrl: 'https://host' }, 'gpt')
  socket.open()
  socket.message({
    type: 'server.hello',
    status: 'ready',
    transport_id: 'transport-server',
    protocol: WIRE_CONTRACT.protocol,
    method_set_hash: methodSetHash(),
    capability_generation: 9,
    binding_generation: 11,
    sid: 'sid-1'
  })

  const send = {
    type: 'browser.cdp.send',
    sid: 'sid-1',
    profile: 'gpt',
    capability_generation: 9,
    binding_generation: 11,
    relay_token: 'R'.repeat(43),
    task_id: 'task-1',
    tab_id: 'browser:tab-1',
    guest_generation: 'guest-1',
    role: 'automation',
    task_generation: 3,
    operation_id: 'relay:1',
    frame: { id: 7, method: 'Page.navigate', params: { url: 'https://example.test/' } }
  }

  socket.message(send)
  await Promise.resolve()
  await Promise.resolve()
  assert.equal(dispatchCalls.length, 1)
  assert.equal(dispatchCalls[0].profile, 'gpt')
  assert.equal(dispatchCalls[0].capabilityGeneration, 9)
  assert.equal(dispatchCalls[0].bindingGeneration, 11)
  assert.equal(typeof dispatchCalls[0].connectionId, 'string')
  assert.ok(dispatchCalls[0].connectionId.length > 0)
  const response = JSON.parse(socket.sent.at(-1)!)
  assert.equal(response.type, 'browser.cdp.frame')
  assert.equal(response.operation_id, 'relay:1')
  assert.deepEqual(response.frame, { id: 7, result: { frameId: 'frame-1' } })
  assert.deepEqual(
    [response.task_id, response.tab_id, response.guest_generation, response.role, response.task_generation],
    ['task-1', 'browser:tab-1', 'guest-1', 'automation', 3]
  )

  assert.equal(
    client.forwardCdpEvent({
      frame: { method: 'Page.loadEventFired', params: { timestamp: 1 } },
      guestGeneration: 'guest-1',
      role: 'automation',
      tabId: 'browser:tab-1',
      taskGeneration: 3,
      taskId: 'task-1'
    }),
    true
  )
  const event = JSON.parse(socket.sent.at(-1)!)
  assert.equal(event.operation_id, null)
  assert.deepEqual(event.frame, { method: 'Page.loadEventFired', params: { timestamp: 1 } })

  socket.message({ ...send, binding_generation: 12 })
  await Promise.resolve()
  await Promise.resolve()
  assert.deepEqual(socket.closeCalls.at(-1), [4400, 'invalid browser operational frame'])
  assert.equal(dispatchCalls.length, 1)
})

test('duplicate operation ids dispatch once and a late result cannot cross into a successor socket', async () => {
  const sockets: FakeWebSocket[] = []
  const pending = deferred<Record<string, unknown>>()
  let dispatchCount = 0
  let mintCount = 0

  const client = new BrowserDarkClient({
    association: new DesktopConnectionAssociation(() => Buffer.alloc(32, 18)),
    mintTicket: async () => `ticket-${++mintCount}`,
    createWebSocket: () => {
      const socket = new FakeWebSocket()
      sockets.push(socket)

      return socket as any
    },
    setInterval: (() => 1) as any,
    clearInterval: (() => undefined) as any,
    localEnabled: () => true,
    dispatchCdp: () => {
      dispatchCount += 1

      return pending.promise
    }
  })

  const ready = (socket: FakeWebSocket, sid: string, generation: number) => {
    socket.open()
    socket.message({
      type: 'server.hello',
      status: 'ready',
    transport_id: 'transport-server',
      protocol: WIRE_CONTRACT.protocol,
      method_set_hash: methodSetHash(),
      capability_generation: generation,
      binding_generation: generation,
      sid
    })
  }

  await client.connect({ baseUrl: 'https://host' }, 'gpt')
  ready(sockets[0], 'sid-old', 1)
  const send = {
    type: 'browser.cdp.send',
    sid: 'sid-old',
    profile: 'gpt',
    capability_generation: 1,
    binding_generation: 1,
    relay_token: 'R'.repeat(43),
    task_id: 'task-race',
    tab_id: 'browser:tab-race',
    guest_generation: 'guest-race',
    role: 'automation',
    task_generation: 1,
    operation_id: 'operation-race',
    frame: { id: 9, method: 'Page.getFrameTree', params: {} }
  }

  sockets[0].message(send)
  sockets[0].message(send)
  await Promise.resolve()
  assert.equal(dispatchCount, 1)

  await client.connect({ baseUrl: 'https://host' }, 'gpt')
  ready(sockets[1], 'sid-new', 2)
  const successorFramesBefore = sockets[1].sent.length
  pending.resolve({ id: 9, result: { frameTree: {} } })
  await Promise.resolve()
  await Promise.resolve()

  assert.equal(sockets[0].sent.some(row => JSON.parse(row).operation_id === 'operation-race'), false)
  assert.equal(sockets[1].sent.length, successorFramesBefore)
})

test('exact relay close control cancels one token without dispatching CDP or forwarding a late result', async () => {
  const socket = new FakeWebSocket()
  const pending = deferred<Record<string, unknown>>()
  let dispatchCount = 0
  const client = new BrowserDarkClient({
    association: new DesktopConnectionAssociation(() => Buffer.alloc(32, 19)),
    mintTicket: async () => 'ticket',
    createWebSocket: () => socket as any,
    setInterval: (() => 1) as any,
    clearInterval: (() => undefined) as any,
    localEnabled: () => true,
    dispatchCdp: request => {
      dispatchCount += 1
      assert.equal(request.remainingDurationMs, 29_000)
      return pending.promise
    }
  })

  await client.connect({ baseUrl: 'https://host' }, 'gpt')
  socket.open()
  socket.message({
    type: 'server.hello',
    status: 'ready',
    transport_id: 'transport-server',
    protocol: WIRE_CONTRACT.protocol,
    method_set_hash: methodSetHash(),
    capability_generation: 4,
    binding_generation: 5,
    sid: 'sid-close'
  })
  const route = {
    type: 'browser.cdp.send',
    sid: 'sid-close',
    profile: 'gpt',
    capability_generation: 4,
    binding_generation: 5,
    relay_token: 'C'.repeat(43),
    task_id: 'task-close',
    tab_id: 'tab-close',
    guest_generation: 'guest-close',
    role: 'automation',
    task_generation: 1
  }
  socket.message({
    ...route,
    operation_id: 'operation-close',
    remaining_duration_ms: 29_000,
    frame: { id: 17, method: 'Runtime.evaluate', params: { expression: '1' } }
  })
  await Promise.resolve()
  assert.equal(dispatchCount, 1)

  socket.message({ ...route, operation_id: null, frame: { type: 'browser.relay.close' } })
  await Promise.resolve()
  assert.equal(dispatchCount, 1)
  const sentBeforeLateResult = socket.sent.length
  pending.resolve({ id: 17, result: { value: 1 } })
  await Promise.resolve()
  await Promise.resolve()
  assert.equal(socket.sent.length, sentBeforeLateResult)
})

test('renderer task lifecycle flushes after authenticated ready and fences stale teardown', async () => {
  const socket = new FakeWebSocket()

  const client = new BrowserDarkClient({
    association: new DesktopConnectionAssociation(() => Buffer.alloc(32, 15)),
    mintTicket: async () => 'ticket',
    createWebSocket: () => socket as any,
    setInterval: (() => 1) as any,
    clearInterval: (() => undefined) as any,
    localEnabled: () => true
  })

  const first = {
    guestGeneration: 'guest-1',
    profile: 'gpt',
    tabId: 'browser:tab-1',
    taskGeneration: 1,
    taskId: 'task-life'
  }

  assert.equal(client.bindTask(first), true)
  await client.connect({ baseUrl: 'https://host' }, 'gpt')

  socket.open()
  socket.message({
    type: 'server.hello',
    status: 'ready',
    transport_id: 'transport-server',
    protocol: WIRE_CONTRACT.protocol,
    method_set_hash: methodSetHash(),
    capability_generation: 1,
    binding_generation: 1,
    sid: 'sid-1'
  })
  assert.deepEqual(JSON.parse(socket.sent.at(-1)!), {
    type: 'client.task.bind',
    task_id: 'task-life',
    tab_id: 'browser:tab-1',
    guest_generation: 'guest-1',
    task_generation: 1
  })

  socket.message({
    type: 'browser.outcome',
    status: 'browser_task_already_bound',
    retryable: true,
    delivery: 'not_started',
    capability_generation: 1
  })
  assert.equal(client.status.state, 'ready')
  assert.equal(socket.closeCalls.length, 0)

  const second = { ...first, guestGeneration: 'guest-2', taskGeneration: 2 }
  assert.equal(client.bindTask(second), true)
  assert.equal(client.unbindTask(first), false)
  assert.equal(client.unbindTask(second), true)
  assert.equal(JSON.parse(socket.sent.at(-1)!).type, 'client.task.unbind')
  assert.equal(client.bindTask(second), false)
})

test('profile switch retires old bindings while same-profile reconnect replays them', async () => {
  const sockets: FakeWebSocket[] = []

  const client = new BrowserDarkClient({
    association: new DesktopConnectionAssociation(() => Buffer.alloc(32, 16)),
    mintTicket: async () => 'ticket',
    createWebSocket: () => {
      const socket = new FakeWebSocket()
      sockets.push(socket)

      return socket as any
    },
    setInterval: (() => 1) as any,
    clearInterval: (() => undefined) as any,
    localEnabled: () => true
  })

  const binding = {
    guestGeneration: 'guest-a',
    profile: 'profile-a',
    tabId: 'tab-a',
    taskGeneration: 1,
    taskId: 'task-a'
  }

  const ready = (socket: FakeWebSocket, sid: string, generation: number) => {
    socket.open()
    socket.message({
      type: 'server.hello',
      status: 'ready',
    transport_id: 'transport-server',
      protocol: WIRE_CONTRACT.protocol,
      method_set_hash: methodSetHash(),
      capability_generation: generation,
      binding_generation: generation,
      sid
    })
  }

  assert.equal(client.bindTask(binding), true)
  await client.connect({ baseUrl: 'https://host' }, 'profile-a')
  ready(sockets[0], 'sid-a', 1)
  assert.equal(JSON.parse(sockets[0].sent.at(-1)!).type, 'client.task.bind')

  await client.connect({ baseUrl: 'https://host' }, 'profile-a')
  ready(sockets[1], 'sid-a-2', 2)
  assert.equal(JSON.parse(sockets[1].sent.at(-1)!).type, 'client.task.bind')

  await client.connect({ baseUrl: 'https://host' }, 'profile-b')
  ready(sockets[2], 'sid-b', 3)
  assert.equal(sockets[2].sent.some(row => JSON.parse(row).type === 'client.task.bind'), false)
  assert.equal(client.unbindTask(binding), false)
})
