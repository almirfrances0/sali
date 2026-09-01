---
name: Next.js
tags:
  - nextjs
  - next
  - react
  - typescript
  - node
  - npm
  - vercel
---

# Next.js

## Scaffolding
- `npx create-next-app@latest .` inside the task workspace (trailing `.` — create in the workspace, not a new folder). Answer the prompts (TypeScript, App Router, Tailwind) per the request.
- `npm run dev` (dev server), `npm run build` (production build), `npm start` (serve the build).

## Structure (App Router)
- Routes are folders under `app/`; `page.tsx` renders a route, `layout.tsx` wraps it, `route.ts` is an API handler.
- Server Components by default; add `"use client"` only where interactivity needs it.
- Data fetching in Server Components with `fetch` (cached by default) or a DB client.

## Verification
- `npm run build` succeeds (this catches type errors and bad imports) — the evidence a change is sound.
- The dev/prod server starts and the expected route responds.

## Gotchas
- Hydration mismatch → server and client rendered different markup; usually a `Date.now()`/random/`window` used during render.
- A blank page with a build that "passed" → check the route file names and the `app/` structure.
