import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createRoot } from 'react-dom/client'

import App from './App'
import './index.css'

// No refetch loops: the dashboard is driven by Sali's event bus (the /stream firehose), exactly like the
// terminal/daemon. Snapshots are read once, then refreshed only when a real event fires.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 10_000, retry: 1, refetchOnWindowFocus: false, refetchOnReconnect: false },
  },
})

// StrictMode is intentionally omitted: its dev-only double-mount reconnects the live WebSockets twice,
// which reads as "loading things twice". Without it, dev behaves like production — one connection each.
const root = document.getElementById('root')
if (root) {
  createRoot(root).render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  )
}
