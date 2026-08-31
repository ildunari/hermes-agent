export interface PluginStyleHandle {
  dispose(): void
  replace(css: string): void
}

export interface PluginStyles {
  /** Add CSS constrained to a host-owned root scope. The style element and its
   * scope marker are removed when the handle or plugin is disposed. */
  add(id: string, css: string): PluginStyleHandle
}

let styleSequence = 0

function safeToken(value: string): string {
  return (
    value
      .toLowerCase()
      .replace(/[^a-z0-9-]+/g, '-')
      .replace(/^-+|-+$/g, '') || 'plugin'
  )
}

function scopedCss(attribute: string, css: string): string {
  // Imports and namespaces are stylesheet-global and cannot be meaningfully
  // constrained by @scope. Runtime plugins get a removable style contribution,
  // not an escape hatch into the app stylesheet.
  if (/@(?:import|namespace)\b/i.test(css)) {
    throw new Error('Plugin styles cannot contain @import or @namespace rules')
  }

  let depth = 0
  let quote = ''
  let comment = false

  for (let index = 0; index < css.length; index += 1) {
    const char = css[index]
    const next = css[index + 1]

    if (comment) {
      if (char === '*' && next === '/') {
        comment = false
        index += 1
      }

      continue
    }

    if (quote) {
      if (char === '\\') {
        index += 1
      } else if (char === quote) {
        quote = ''
      }

      continue
    }

    if (char === '/' && next === '*') {
      comment = true
      index += 1
    } else if (char === '"' || char === "'") {
      quote = char
    } else if (char === '{') {
      depth += 1
    } else if (char === '}') {
      depth -= 1

      if (depth < 0) {
        throw new Error('Plugin styles must remain inside their host scope')
      }
    }
  }

  if (depth !== 0 || quote || comment) {
    throw new Error('Plugin styles contain an unterminated rule, string, or comment')
  }

  return `@scope (:root[${attribute}]) {\n${css}\n}`
}

export function createPluginStyles(
  pluginId: string,
  track: (dispose: () => void) => () => void,
  documentRef: Document | undefined = typeof document === 'undefined' ? undefined : document
): PluginStyles {
  return {
    add(id, initialCss) {
      if (!documentRef) {
        const unavailable: PluginStyleHandle = { dispose: () => undefined, replace: () => undefined }
        track(unavailable.dispose)

        return unavailable
      }

      const sequence = ++styleSequence
      const attribute = `data-hermes-plugin-style-${safeToken(pluginId)}-${safeToken(id)}-${sequence}`
      const style = documentRef.createElement('style')
      const initialScopedCss = scopedCss(attribute, String(initialCss || ''))
      let disposed = false

      style.dataset.hermesPluginStyle = `${pluginId}:${id}`
      style.textContent = initialScopedCss
      documentRef.documentElement.setAttribute(attribute, '')
      documentRef.head.append(style)

      const replace = (css: string) => {
        if (!disposed) {
          style.textContent = scopedCss(attribute, String(css || ''))
        }
      }

      const dispose = () => {
        if (disposed) {
          return
        }

        disposed = true
        style.remove()
        documentRef.documentElement.removeAttribute(attribute)
      }

      track(dispose)

      return { dispose, replace }
    }
  }
}
