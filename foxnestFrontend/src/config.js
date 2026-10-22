// API Configuration — prefer Vite env; fall back to local backend, never a LAN IP.
export const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://127.0.0.1:33333/api'
// Empty is kept, not replaced: it means the origin that served the page. The production
// build behind nginx (deploy.sh) sets it so, and `${API_SERVER_URL}/api/...` stays /api/...
export const API_SERVER_URL = import.meta.env.VITE_API_SERVER_URL ?? 'http://127.0.0.1:33333'
