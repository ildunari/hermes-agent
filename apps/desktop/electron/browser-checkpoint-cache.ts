const DEFAULT_TTL_MS = 15 * 60 * 1_000
const MAX_PER_TAB = 3
const MAX_PER_PROFILE = 16
const MAX_BYTES_PER_PROFILE = 64 * 1024 * 1024

export interface BrowserCheckpointInput {
  bytes: Uint8Array
  decodedBytes?: number
  id?: string
  private?: boolean
  profile: string
  tabIncarnationId: string
}

export interface BrowserCheckpointMetadata {
  capturedAt: number
  expiresAt: number
  id: string
  profile: string
  sizeBytes: number
  tabIncarnationId: string
}

interface BrowserCheckpointEntry extends BrowserCheckpointMetadata {
  bytes: Uint8Array
  timer?: ReturnType<typeof setTimeout>
}

export class BrowserCheckpointCache {
  #entries = new Map<string, BrowserCheckpointEntry>()
  #now: () => number
  #ttlMs: number

  constructor(options: { now?: () => number; ttlMs?: number } = {}) {
    this.#now = options.now ?? Date.now
    this.#ttlMs = options.ttlMs ?? DEFAULT_TTL_MS
  }

  put(input: BrowserCheckpointInput): BrowserCheckpointMetadata | null {
    if (!(input.bytes instanceof Uint8Array) || input.bytes.byteLength < 1) {return null}
    const decodedBytes = input.decodedBytes ?? 0

    if (!Number.isSafeInteger(decodedBytes) || decodedBytes < 0) {return null}
    const sizeBytes = input.bytes.byteLength + decodedBytes

    if (sizeBytes > MAX_BYTES_PER_PROFILE) {return null}
    const capturedAt = this.#now()
    const id = input.id ?? globalThis.crypto.randomUUID()

    if (!id || id.length > 160 || this.#entries.has(id)) {return null}

    const entry: BrowserCheckpointEntry = {
      bytes: new Uint8Array(input.bytes),
      capturedAt,
      expiresAt: capturedAt + this.#ttlMs,
      id,
      profile: input.profile,
      sizeBytes,
      tabIncarnationId: input.tabIncarnationId
    }

    this.#entries.set(id, entry)
    entry.timer = setTimeout(() => this.delete(id), this.#ttlMs)
    entry.timer.unref?.()
    this.#enforceBounds(input.profile, input.tabIncarnationId)

    return this.metadata(id)
  }

  get(id: string): Uint8Array | null {
    const entry = this.#entries.get(id)

    if (!entry) {return null}

    if (entry.expiresAt <= this.#now()) {
      this.delete(id)

      return null
    }

    return new Uint8Array(entry.bytes)
  }

  metadata(id: string): BrowserCheckpointMetadata | null {
    const entry = this.#entries.get(id)

    if (!entry || entry.expiresAt <= this.#now()) {
      if (entry) {this.delete(id)}

      return null
    }

    const { capturedAt, expiresAt, profile, sizeBytes, tabIncarnationId } = entry

    return { capturedAt, expiresAt, id, profile, sizeBytes, tabIncarnationId }
  }

  delete(id: string): boolean {
    const entry = this.#entries.get(id)

    if (!entry) {return false}

    if (entry.timer) {clearTimeout(entry.timer)}
    entry.bytes.fill(0)
    this.#entries.delete(id)

    return true
  }

  evictTab(profile: string, tabIncarnationId: string): void {
    this.#evictMatching(entry => entry.profile === profile && entry.tabIncarnationId === tabIncarnationId)
  }

  evictTabAcrossProfiles(tabIncarnationId: string): void {
    this.#evictMatching(entry => entry.tabIncarnationId === tabIncarnationId)
  }

  evictProfile(profile: string): void { this.#evictMatching(entry => entry.profile === profile) }
  evictAll(): void { this.#evictMatching(() => true) }
  handleMemoryPressure(profile?: string): void { profile ? this.evictProfile(profile) : this.evictAll() }

  list(profile: string): BrowserCheckpointMetadata[] {
    this.#purgeExpired()

    return this.#oldest(profile).map(({ bytes: _bytes, timer: _timer, ...metadata }) => metadata)
  }

  #evictMatching(predicate: (entry: BrowserCheckpointEntry) => boolean): void {
    for (const entry of this.#entries.values()) {if (predicate(entry)) {this.delete(entry.id)}}
  }

  #purgeExpired(): void {
    const now = this.#now()

    for (const entry of this.#entries.values()) {if (entry.expiresAt <= now) {this.delete(entry.id)}}
  }

  #oldest(profile: string): BrowserCheckpointEntry[] {
    return [...this.#entries.values()]
      .filter(entry => entry.profile === profile)
      .sort((left, right) => left.capturedAt - right.capturedAt || left.id.localeCompare(right.id))
  }

  #enforceBounds(profile: string, tabIncarnationId: string): void {
    const tabEntries = this.#oldest(profile).filter(entry => entry.tabIncarnationId === tabIncarnationId)

    while (tabEntries.length > MAX_PER_TAB) {this.delete(tabEntries.shift()!.id)}

    let profileEntries = this.#oldest(profile)

    while (profileEntries.length > MAX_PER_PROFILE) {
      this.delete(profileEntries.shift()!.id)
    }

    profileEntries = this.#oldest(profile)
    let total = profileEntries.reduce((sum, entry) => sum + entry.sizeBytes, 0)

    while (total > MAX_BYTES_PER_PROFILE && profileEntries.length) {
      const oldest = profileEntries.shift()!
      total -= oldest.sizeBytes
      this.delete(oldest.id)
    }
  }
}
