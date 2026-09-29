import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { getToken, login as apiLogin, logout as apiLogout, ApiError } from '../api'

interface AuthState {
  token: string | null
  ready: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

/**
 * Holds the bearer token. The SPA reuses the same localStorage key the old
 * Jinja pages used, so an existing session survives the migration and users
 * are not logged out by the switch.
 *
 * `ready` exists because the token is read from localStorage after the first
 * render; routing must not bounce to /login before that read completes, or a
 * signed-in user would see the login page flash on every reload.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTokenState] = useState<string | null>(null)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    setTokenState(getToken())
    setReady(true)
  }, [])

  const login = useCallback(async (username: string, password: string) => {
    const issued = await apiLogin(username, password)
    setTokenState(issued)
  }, [])

  const logout = useCallback(async () => {
    await apiLogout()
    setTokenState(null)
  }, [])

  const value = useMemo<AuthState>(
    () => ({ token, ready, login, logout }),
    [token, ready, login, logout],
  )
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider')
  return ctx
}

export { ApiError }
