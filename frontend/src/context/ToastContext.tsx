import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'

export type ToastKind = 'success' | 'danger' | 'warning' | 'info'

interface Toast {
  id: number
  message: string
  kind: ToastKind
}

interface ToastApi {
  push: (message: string, kind?: ToastKind) => void
}

const ToastContext = createContext<ToastApi | null>(null)

const ICONS: Record<ToastKind, string> = {
  success: 'bi-check-circle-fill',
  danger: 'bi-x-circle-fill',
  warning: 'bi-exclamation-triangle-fill',
  info: 'bi-info-circle-fill',
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const nextId = useRef(1)

  const push = useCallback((message: string, kind: ToastKind = 'info') => {
    const id = nextId.current++
    setToasts((current) => [...current, { id, message, kind }])
    window.setTimeout(() => {
      setToasts((current) => current.filter((t) => t.id !== id))
    }, 5000)
  }, [])

  const value = useMemo<ToastApi>(() => ({ push }), [push])

  return (
    <ToastContext.Provider value={value}>
      {children}
      {/* aria-live so screen readers announce transient results. */}
      <div className="kvuno-toast-stack" aria-live="polite" role="status">
        {toasts.map((toast) => (
          <div key={toast.id} className={`alert alert-${toast.kind} kvuno-toast py-2 px-3`}>
            <i className={`bi ${ICONS[toast.kind]} me-2`} />
            {toast.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used inside ToastProvider')
  return ctx
}
