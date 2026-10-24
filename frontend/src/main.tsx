import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import 'bootstrap/dist/css/bootstrap.min.css'
import 'bootstrap-icons/font/bootstrap-icons.css'
// Self-hosted (CSP font-src 'self'): titles, body, and data.
import '@fontsource-variable/bricolage-grotesque'
import '@fontsource-variable/public-sans'
import '@fontsource/ibm-plex-mono/400.css'
import '@fontsource/ibm-plex-mono/500.css'
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
