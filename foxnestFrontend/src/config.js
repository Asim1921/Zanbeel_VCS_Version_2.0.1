// API Configuration — prefer Vite env; fall back to local backend, never a LAN IP.
export const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://127.0.0.1:33333/api'
export const API_SERVER_URL = import.meta.env.VITE_API_SERVER_URL || 'http://127.0.0.1:33333'
