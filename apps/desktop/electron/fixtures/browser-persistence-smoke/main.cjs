const { app, session } = require('electron')
const fs = require('node:fs')

const userData = process.env.HERMES_BROWSER_SMOKE_USER_DATA
const phase = process.env.HERMES_BROWSER_SMOKE_PHASE
const persistentPartition = 'persist:hermes-browser:v1:smoke-profile-scope'
const privatePartition = 'hermes-browser-private:v1:00000000-0000-4000-8000-000000000001'
const url = 'https://scope.test/'

if (!userData || !['seed', 'verify'].includes(phase)) {
  process.stderr.write('missing smoke phase/userData\n')
  process.exit(2)
}

fs.mkdirSync(userData, { recursive: true })
app.setPath('userData', userData)

app.whenReady().then(async () => {
  const persistent = session.fromPartition(persistentPartition)
  const privateSession = session.fromPartition(privatePartition)

  if (phase === 'seed') {
    await persistent.cookies.set({ expirationDate: 2_000_000_000, name: 'profile_cookie', url, value: 'persistent' })
    await privateSession.cookies.set({ expirationDate: 2_000_000_000, name: 'private_cookie', url, value: 'ephemeral' })
    persistent.flushStorageData()
    process.stdout.write(JSON.stringify({ seeded: true }) + '\n')
    app.quit()
    return
  }

  const persistentCookies = await persistent.cookies.get({ url })
  const privateCookies = await privateSession.cookies.get({ url })
  process.stdout.write(JSON.stringify({
    electron: process.versions.electron,
    persistent: persistentCookies.some(cookie => cookie.name === 'profile_cookie' && cookie.value === 'persistent'),
    private: privateCookies.some(cookie => cookie.name === 'private_cookie')
  }) + '\n')
  app.quit()
}).catch(error => {
  process.stderr.write(String(error && error.stack || error) + '\n')
  app.exit(1)
})
