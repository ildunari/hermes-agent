import { normalizeProfileKey } from '@/store/profile'

const PROFILE_SCOPE_DOMAIN = 'hermes-browser-profile-v1\0'
const BASE64URL_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'

function base64Url(bytes: Uint8Array): string {
  let encoded = ''

  for (let index = 0; index < bytes.length; index += 3) {
    const first = bytes[index]
    const second = bytes[index + 1]
    const third = bytes[index + 2]
    const value = (first << 16) | ((second ?? 0) << 8) | (third ?? 0)

    encoded += BASE64URL_ALPHABET[(value >>> 18) & 63]
    encoded += BASE64URL_ALPHABET[(value >>> 12) & 63]

    if (second !== undefined) {
      encoded += BASE64URL_ALPHABET[(value >>> 6) & 63]
    }

    if (third !== undefined) {
      encoded += BASE64URL_ALPHABET[value & 63]
    }
  }

  return encoded
}

export async function browserProfileScope(profile: string): Promise<string> {
  // Match hermes_cli.profiles.normalize_profile_name(): profile identity is
  // trimmed and case-insensitive. Keeping this canonicalization at the browser
  // boundary avoids changing renderer display keys while ensuring one logical
  // profile can own only one persistent Chromium partition.
  const normalized = normalizeProfileKey(profile).toLowerCase()
  const input = new TextEncoder().encode(`${PROFILE_SCOPE_DOMAIN}${normalized}`)
  const digest = await globalThis.crypto.subtle.digest('SHA-256', input)

  return base64Url(new Uint8Array(digest)).slice(0, 22)
}

export async function browserPartitionForProfile(profile: string): Promise<string> {
  return `persist:hermes-browser:v1:${await browserProfileScope(profile)}`
}
