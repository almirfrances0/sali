---
name: Next.js
tags: [nextjs, next, react, ssr, ssg, app-router, server-components]
dependencies: [react, typescript]
version_hint: next@15
project_detect:
  - next.config.js
  - next.config.mjs
  - next.config.ts
  - app/layout.tsx
  - pages/_app.tsx
summary: Next.js is React + server rendering + routing + build. In v13+, the App Router (`app/`) is default; Pages Router (`pages/`) is legacy but not deprecated. Server Components render on the server (no JS shipped); Client Components (`"use client"`) hydrate. Data fetching via async Server Components; mutations via Server Actions. Caching is aggressive — understand fetch cache, route cache, full-route cache. Deploy on Vercel or Node self-hosted; never assume Vercel-only features when self-hosting.
---

# Next.js (App Router, v15)

## Version detection

`package.json` → `"next": "^15.0.0"` (or 14.x, 13.x). App Router (`app/` directory) is
default in 13+; Pages Router (`pages/` directory) still works but is legacy. If both exist,
they coexist but the App Router takes precedence for matching routes.

## Server vs Client Components

    // app/dashboard/page.tsx — server component (default, no directive)
    export default async function DashboardPage() {
        const stats = await fetchStats();   // runs on server; result serialised to client
        return <StatsPanel stats={stats} />;
    }

    // components/InteractiveThing.tsx — client component
    "use client";
    import { useState } from "react";
    export function InteractiveThing() { ... }

* Server Components run ONCE on the server, produce HTML, ship ZERO JS for that component.
* Client Components hydrate — bundle includes their JS.
* A Client Component can render a Server Component ONLY via children props, not by direct import.
* Anything that uses hooks (`useState`, `useEffect`, `useContext`) is a Client Component.
* Prefer Server; escalate to Client only when interactivity actually needs it.

## Routing

    app/
      layout.tsx           # root layout (server, always renders)
      page.tsx             # /
      loading.tsx          # /*loading state (built-in suspense boundary)
      error.tsx            # /*error boundary (must be "use client")
      not-found.tsx
      dashboard/
        layout.tsx         # /dashboard/* wraps children
        page.tsx           # /dashboard
        [id]/page.tsx      # /dashboard/:id
        (marketing)/       # route group — does not affect URL
      api/
        route.ts           # HTTP handler at /api/route

* File-based routing; folders are segments; special filenames are conventions.
* `layout.tsx` composes down the tree — root layout wraps every page.
* `loading.tsx` auto-triggers a Suspense boundary for its segment; users see it during fetch.
* `error.tsx` catches thrown errors in its segment; must be a Client Component.
* Route groups `(name)` for organisation without URL impact.

## Data fetching

    // Server Component — await directly
    export default async function Page({ params }: { params: Promise<{ id: string }> }) {
        const { id } = await params;   // v15: params is a Promise
        const post = await fetch(`https://api.example.com/posts/${id}`, {
            next: { revalidate: 60 }   // ISR: cache for 60s
        }).then(r => r.json());
        return <Article post={post} />;
    }

* v15 changed `params` and `searchParams` from objects to Promises — `await params`.
* `fetch()` in a Server Component is DEDUPED per render pass.
* Cache options:
  * `next: { revalidate: 60 }` — ISR, refetch after 60s.
  * `cache: 'no-store'` — dynamic on every request.
  * `next: { tags: ['posts'] }` — tag-based revalidation via `revalidateTag('posts')`.
* Never `fetch()` inside a Client Component if the data is stable — use a Server Component
  parent and pass data down.

## Server Actions (mutations)

    "use server";
    export async function createPost(formData: FormData) {
        const title = formData.get("title") as string;
        await db.post.create({ data: { title } });
        revalidatePath("/posts");
    }

    // In a Client Component form:
    <form action={createPost}>
        <input name="title" />
        <button type="submit">Create</button>
    </form>

* Progressive enhancement: forms work without JS if the action is a Server Action.
* Always call `revalidatePath` / `revalidateTag` after a mutation to bust the cache.
* Validate inputs — `formData.get()` returns `FormDataEntryValue | null`; type-coerce and
  validate with Zod.

## Middleware

    // middleware.ts (root)
    import { NextResponse } from "next/server";
    export function middleware(request: NextRequest) {
        if (!request.cookies.get("session")) {
            return NextResponse.redirect(new URL("/login", request.url));
        }
    }
    export const config = { matcher: ["/dashboard/:path*"] };

* Runs BEFORE route matching, on the Edge Runtime (v8, not Node).
* No access to Node APIs (`fs`, `crypto` with certain algorithms).
* Good for: auth redirects, A/B routing, geo-based routing, request rewrites.
* Not for: complex business logic (belongs in a Route Handler or Server Action).

## Metadata / SEO

    export const metadata: Metadata = {
        title: "Dashboard | MyApp",
        description: "…",
        openGraph: { images: [{ url: "/og.png" }] },
    };

* Static metadata for stable pages; `generateMetadata()` for dynamic (uses params, fetches).
* Every page should export metadata for SEO + social previews.

## Caching layers (v15 knowledge)

Next.js has 4 caches. Understand them or debug forever.

1. **Request memoization**: same `fetch()` in the same render pass is deduped.
2. **Data cache**: `fetch()` results persist across requests, keyed by URL + options. Controlled
   by `revalidate` and `cache`.
3. **Full route cache**: static routes render once at build; served from cache.
4. **Router cache** (client): the client keeps prefetched routes in memory.

Cache is OFF for a route if: any `cookies()`, `headers()`, `noStore()`, or `fetch(..., cache:
'no-store')` is used. That makes the route DYNAMIC.

## Deployment

* **Vercel**: zero-config, uses all the features. Preview per PR. Analytics + Web Vitals free.
* **Self-host (Node)**: `next build && next start`. Provide your own reverse proxy, static
  asset CDN, ISR revalidation strategy.
* **Standalone output** (`output: 'standalone'` in `next.config`): produces a minimal Node
  server + dependencies for Docker.

## Anti-patterns

* `"use client"` at the top of a page for no reason — you just shipped React and lost server
  rendering.
* `useState` for data that could be server-fetched.
* Fetching in `useEffect` in App Router — that's a Server Component job.
* Ignoring cache — `fetch(url)` in a loop under `no-store` is 1000 requests.
* Editing `pages/*` when the project is on the App Router — coexists but confuses.
* `params` used synchronously in v15 — must `await`.
* Long-running work in a Server Action — it blocks the response. Enqueue a job.

## Verification

* `next build` — succeeds with no errors, no dynamic warnings for pages meant to be static.
* Bundle analysis: `@next/bundle-analyzer`. Client bundle for a page should be < 200 KB
  gzipped for anything user-facing.
* Lighthouse: LCP < 2.5s on a real (not localhost) network.
* All pages have `metadata` exports.
