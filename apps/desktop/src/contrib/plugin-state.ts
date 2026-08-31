import type { PluginStorage } from './plugin'

export interface PluginStateOptions<T> {
  defaultValue: T
  key: string
  migrate?: (value: unknown, fromVersion: number) => T
  validate?: (value: unknown) => value is T
  version: number
}

export interface PluginState<T> {
  get(): T
  set(value: T): void
  subscribe(listener: (value: T) => void): () => void
  update(updater: (value: T) => T): void
  readonly version: number
}

interface StateEnvelope {
  value: unknown
  version: number
}

function isEnvelope(value: unknown): value is StateEnvelope {
  if (!value || typeof value !== 'object') {
    return false
  }

  const record = value as Record<string, unknown>

  return Number.isInteger(record.version) && Number(record.version) >= 0 && 'value' in record
}

export function createPluginStateFactory(storage: PluginStorage) {
  return function create<T>(options: PluginStateOptions<T>): PluginState<T> {
    if (!options.key.trim() || !Number.isInteger(options.version) || options.version < 1) {
      throw new Error('Plugin state requires a key and a positive integer version')
    }

    const storageKey = `state.${options.key}`
    const stored = storage.get<unknown>(storageKey, null)
    let current = options.defaultValue

    if (isEnvelope(stored)) {
      if (stored.version === options.version && (!options.validate || options.validate(stored.value))) {
        current = stored.value as T
      } else if (options.migrate) {
        const migrated = options.migrate(stored.value, stored.version)

        if (options.validate && !options.validate(migrated)) {
          throw new Error(`Plugin state "${options.key}" migration returned an invalid value`)
        }

        current = migrated
        storage.set(storageKey, { value: current, version: options.version })
      }
    }

    const listeners = new Set<(value: T) => void>()

    const set = (value: T) => {
      if (options.validate && !options.validate(value)) {
        throw new Error(`Plugin state "${options.key}" rejected an invalid value`)
      }

      current = value
      storage.set(storageKey, { value, version: options.version })
      listeners.forEach(listener => listener(value))
    }

    return {
      get: () => current,
      set,
      subscribe(listener) {
        listeners.add(listener)

        return () => listeners.delete(listener)
      },
      update: updater => set(updater(current)),
      version: options.version
    }
  }
}
