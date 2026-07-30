export const SESSION_USERNAME_KEY = 'foxnest_username'
export const SESSION_ROLE_KEY = 'foxnest_role'
export const SESSION_TOKEN_KEY = 'foxnest_token'

export const getSessionUsername = () => {
  const value = window.localStorage.getItem(SESSION_USERNAME_KEY)
  return value ? value.trim() : ''
}

export const getSessionRole = () => {
  const value = window.localStorage.getItem(SESSION_ROLE_KEY)
  return value ? value.trim().toLowerCase() : 'developer'
}

export const getSessionToken = () => {
  const value = window.localStorage.getItem(SESSION_TOKEN_KEY)
  return value ? value.trim() : ''
}

export const setSessionUser = ({ username, role, token }) => {
  if (username) window.localStorage.setItem(SESSION_USERNAME_KEY, username)
  if (role) window.localStorage.setItem(SESSION_ROLE_KEY, role)
  if (token) window.localStorage.setItem(SESSION_TOKEN_KEY, token)
}

export const clearSessionUser = () => {
  window.localStorage.removeItem(SESSION_USERNAME_KEY)
  window.localStorage.removeItem(SESSION_ROLE_KEY)
  window.localStorage.removeItem(SESSION_TOKEN_KEY)
}

export const isAdminRole = (role) => ['team_lead', 'admin'].includes((role || '').toLowerCase())

export const getSessionUser = () => {
  const username = getSessionUsername()
  const role = getSessionRole()

  return {
    username,
    role,
    token: getSessionToken(),
    isAdmin: isAdminRole(role)
  }
}
