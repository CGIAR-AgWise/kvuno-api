import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import 'bootstrap/dist/css/bootstrap.min.css'
import 'bootstrap-icons/font/bootstrap-icons.css'
// AKILIMO's typefaces, self-hosted (CSP font-src 'self'): Montserrat for the
// wordmark and titles, Open Sans for text. Latin only, the weights used.
import '@fontsource/montserrat/latin-700.css'
import '@fontsource/open-sans/latin-400.css'
import '@fontsource/open-sans/latin-600.css'
import '@fontsource/open-sans/latin-700.css'
import './lib/theme.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {/* v7_startTransition: navigations run as React transitions, so the page
        on screen stays until the next one is ready instead of being swapped
        for a loading fallback (the flash between pages). */}
    <BrowserRouter future={{ v7_startTransition: true }}>
      {/* AuthProvider lives in App, inside the router; mount it only once. */}
      <App />
    </BrowserRouter>
  </React.StrictMode>,
)
