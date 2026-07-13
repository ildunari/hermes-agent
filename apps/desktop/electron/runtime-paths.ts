import fs from 'node:fs'
import path from 'node:path'

/** Resolve the repository-owned Python environment using the canonical order. */
export function resolveRepoVenvRoot(root: string, exists: (candidate: string) => boolean = fs.existsSync): string {
  const candidates = [path.join(root, '.venv'), path.join(root, 'venv')]

  return candidates.find(exists) ?? candidates[0]
}
