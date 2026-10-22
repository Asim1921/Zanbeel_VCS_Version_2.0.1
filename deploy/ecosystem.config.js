// PM2 process file for the Zanbeel backend: one instance of server/server.py per port
// in deploy/deploy.env (ZANBEEL_API_PORTS), named zanbeel-api-1, zanbeel-api-2, ...
//
// PM2's cluster mode is for Node programs, so each Python instance is its own app on
// its own port, and nginx does the balancing. deploy.sh starts and restarts them one at
// a time; by hand:
//
//   pm2 start deploy/ecosystem.config.js     pm2 ls     pm2 logs zanbeel-api-1

const fs = require('fs')
const path = require('path')

const ROOT = path.resolve(__dirname, '..')

// deploy.env is shell syntax (KEY=value, optionally quoted): deploy.sh sources it.
function readDeployEnv(file) {
  const settings = {}
  if (!fs.existsSync(file)) return settings
  for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
    const match = line.match(/^\s*([A-Z_][A-Z0-9_]*)=(.*)$/)
    if (match) settings[match[1]] = match[2].trim().replace(/^(['"])(.*)\1$/, '$2')
  }
  return settings
}

const settings = readDeployEnv(path.join(__dirname, 'deploy.env'))
const ports = (settings.ZANBEEL_API_PORTS || '8091 8092 8093 8094').split(/\s+/).filter(Boolean)

module.exports = {
  apps: ports.map((port, index) => ({
    name: `zanbeel-api-${index + 1}`,
    // DATABASE_URL defaults to sqlite:///./foxnest.db, relative to this directory.
    cwd: path.join(ROOT, 'server'),
    script: 'server.py',
    interpreter: path.join(ROOT, 'venv', 'bin', 'python'),
    exec_mode: 'fork',
    autorestart: true,
    // Ten crashes in a row, each within 30 seconds of starting, and PM2 stops retrying.
    min_uptime: '30s',
    max_restarts: 10,
    restart_delay: 3000,
    // Stopped with SIGINT; uvicorn finishes the requests in flight, for up to 30 seconds.
    kill_timeout: 30000,
    max_memory_restart: '4G',
    time: true,
    // Set here rather than in server/.env, which load_dotenv() does not let override them.
    env: {
      SERVER_HOST: '127.0.0.1',
      SERVER_PORT: String(port),
      // Every request comes through nginx, which puts the client's address in
      // X-Forwarded-For; sign-in limits and the activity log go by it.
      FOXNEST_TRUST_PROXY: '1',
      FORWARDED_ALLOW_IPS: '127.0.0.1',
      // The https address, for the CLI quick start sent to new collaborators. Without it a
      // notification made outside a request would give this instance's own 127.0.0.1 port.
      FOXNEST_PUBLIC_URL: settings.ZANBEEL_PUBLIC_URL || '',
      PYTHONUNBUFFERED: '1',
      PYTHONIOENCODING: 'utf-8',
      PYTHONUTF8: '1',
    },
  })),
}
