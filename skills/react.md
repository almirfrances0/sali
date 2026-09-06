---
name: React
tags: [react, jsx, hooks, components, ssr]
dependencies: [javascript, typescript]
version_hint: react@18 or 19
project_detect:
  - package.json
summary: React is components + hooks. Function components only in new code; class components are legacy. Composition over inheritance; small components over large ones. Server Components (React 19 / Next.js App Router) render on the server; Client Components hydrate — mark with "use client". State should live at the lowest common ancestor. Escape hatches (useEffect, useRef, forwardRef, memo) exist for a reason but are usually the WRONG first tool.
---

# React (production)

## Version

`react@18` and `react@19` are current. React 18 introduced automatic batching + Suspense in
data fetching. React 19 added Server Components as a first-class concept, `use()` hook,
form actions, `ref` as a prop (no more `forwardRef`).

## Components

    // Function component
    export function UserCard({ user }: { user: User }) {
      return (
        <div>
          <h3>{user.name}</h3>
          <p>{user.email}</p>
        </div>
      );
    }

* Function components. Never write a class component in new code.
* Named export by default (better for refactoring than default export).
* Props typed via TypeScript interface / type; NEVER `any`.

## Hooks

* `useState` — local state.
* `useEffect` — SIDE EFFECTS after render. Usually WRONG for data derivation (use derivation) or
  data fetching (Server Components or a data-fetching lib).
* `useContext` — read a Context; useful for THEME, AUTH, i18n. Don't build your own state
  management on it.
* `useReducer` — when state transitions have complex rules.
* `useMemo` / `useCallback` — memoize; only for MEASURED perf issues. Overuse hurts.
* `useRef` — mutable value that doesn't cause re-render, OR a DOM ref.
* `useId` — SSR-safe unique id (form labels, aria-describedby).

## State placement

State lives at the LOWEST COMMON ANCESTOR of the components that read it. If two children need
it, lift to the parent. If ten components need it, use Context — but only when prop drilling
becomes painful (usually > 3 levels).

Global state managers (Redux, Zustand, Jotai) are for GENUINELY GLOBAL state that changes
frequently. For form state: react-hook-form. For server state: TanStack Query / SWR. Don't put
server data in Redux.

## Server vs Client Components (React 19 / Next)

    // Server Component (default)
    export default async function Posts() {
      const posts = await fetchPosts();
      return posts.map(p => <Post key={p.id} post={p} />);
    }

    // Client Component (opt-in)
    "use client";
    import { useState } from "react";
    export function Counter() { const [n, setN] = useState(0); ... }

* Server Components: run on the server, ship zero JS. Cannot use hooks.
* Client Components: hydrate on the client. Marked with "use client" at top of file (contagious
  to all imports).
* Compose Server → Client is fine; Client → Server via `children` prop only.

## Forms

    "use client";
    import { useForm } from "react-hook-form";
    import { zodResolver } from "@hookform/resolvers/zod";
    import { z } from "zod";

    const schema = z.object({ email: z.string().email(), password: z.string().min(8) });

    export function LoginForm() {
      const { register, handleSubmit, formState: { errors } } = useForm({
        resolver: zodResolver(schema),
      });
      return (
        <form onSubmit={handleSubmit(async (data) => await login(data))}>
          <input {...register("email")} />
          {errors.email && <span>{errors.email.message}</span>}
          <input type="password" {...register("password")} />
          <button type="submit">Sign in</button>
        </form>
      );
    }

* react-hook-form + zod. Never write your own form state.
* Errors near the field, one sentence, in red.
* Submit button lives where the eye lands last.

## Performance

* React is fast by default. Do NOT `useMemo` / `useCallback` / `React.memo` speculatively —
  they have their own overhead.
* If a slow component: profile with React DevTools; find the actual bottleneck.
* Common real fixes: split a large state so unrelated components don't re-render together;
  lift static children out of a dynamic parent; use `key` to force / prevent unmount.
* Never `{items.map(item => <Item key={index} item={item} />)}` with `key={index}` — breaks
  reconciliation when items shift. Use stable IDs.

## Effects (the escape hatch)

`useEffect` is almost always a smell in new code. Alternatives:

* Derived state: `const filtered = items.filter(…)` in render. Not `useEffect` + `setState`.
* Data fetching: Server Component / TanStack Query / SWR. Not `useEffect` + `fetch`.
* Subscriptions: `useSyncExternalStore` (React 18+).
* DOM measurement: `useLayoutEffect` (not `useEffect`) if you must.

Real `useEffect` cases: subscribing to a WebSocket, attaching a global keyboard listener,
integrating with a non-React library. Even these usually have better hooks.

## Accessibility

* Semantic HTML: `<button>` for a button, `<a>` for a link. Not `<div onClick>`.
* Focus visible: don't `outline: none` without a replacement.
* Labels on inputs: `<label htmlFor="email">Email</label> <input id="email" />` or wrap.
* ARIA is the LAST resort — prefer native semantics.

## Anti-patterns

* `key={index}` in a list that reorders.
* State in the parent that only one child reads.
* `useEffect` for data derivation.
* Copying props into state (leads to stale copies).
* Directly mutating state (`state.push(x)` — React won't notice).
* Fetching in a loop without cancellation.
* Big monolithic components (500+ lines) — split.
