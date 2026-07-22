const { app } = require('electron')
const { DatabaseSync } = require('node:sqlite')

app.whenReady().then(() => {
  const db = new DatabaseSync(':memory:')
  db.exec('CREATE TABLE smoke(value INTEGER NOT NULL); INSERT INTO smoke VALUES (42)')
  const value = db.prepare('SELECT value FROM smoke').get().value
  db.close()
  process.stdout.write(JSON.stringify({ electron: process.versions.electron, node: process.versions.node, sqlite: process.versions.sqlite, value }) + '\n')
  app.exit(value === 42 ? 0 : 1)
}).catch(error => {
  process.stderr.write(String(error?.stack || error) + '\n')
  app.exit(1)
})
