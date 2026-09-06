---
name: Tailwind CSS
tags: [tailwind, css, frontend, responsive, vite]
dependencies: [css]
version_hint: tailwind@3 (or tailwind@4 with @import syntax)
project_detect:
  - tailwind.config.js
  - tailwind.config.ts
  - postcss.config.js
  - postcss.config.mjs
summary: Tailwind is utility-first CSS. Style INLINE with utility classes; extract to a component only when the same combination repeats 3+ times. Configure once (theme + content globs); rely on defaults. Mobile-first: unprefixed utilities apply to all sizes, `sm: md: lg: xl:` layer up. Dark mode via `dark:` variant. In v4, tailwind reads config from the CSS layer (`@theme`) rather than `tailwind.config.js`. Never fight Tailwind — hardcoded hex colours / arbitrary values everywhere means the theme is wrong.
---

# Tailwind CSS

## v3 vs v4

* **v3** (widely deployed): `tailwind.config.js` + PostCSS + `content` globs. The scanner walks
  the globs to find used classes; unused classes are purged from the build.
* **v4** (Sep 2024+): CSS-first — `@import "tailwindcss"` in your CSS entry; theme via
  `@theme { --color-primary: ...; }` in CSS; no PostCSS config needed with Vite; content
  detection is automatic via the tooling (no more `content` globs by default).

Detect via `package.json`: `"tailwindcss": "^3.4.0"` vs `"^4.0.0"`. If unclear, check the entry
CSS: `@tailwind base;` = v3; `@import "tailwindcss";` = v4.

Anti-pattern: mixing v3 and v4 configuration — the build silently ignores half your config.

## Setup (v3)

1. `npm install -D tailwindcss postcss autoprefixer && npx tailwindcss init -p`.
2. In `tailwind.config.js`, set `content` to EVERY template path that uses classes:
   `content: ['./index.html', './src/**/*.{ts,tsx,js,jsx}', './resources/**/*.blade.php']`.
   MISSING globs = classes purged = blank page in production.
3. In the CSS entry: `@tailwind base; @tailwind components; @tailwind utilities;`.
4. Run the build — if the page is unstyled, the content glob is wrong.

## Setup (v4)

1. `npm install tailwindcss @tailwindcss/vite` (or the appropriate framework plugin).
2. Add the Vite plugin to `vite.config.ts`.
3. Entry CSS: `@import "tailwindcss";`. Theme with `@theme { ... }`.
4. No content globs — the Vite plugin scans automatically.

## Responsive design

* Mobile-first: unprefixed utilities apply to ALL sizes.
* `sm:` (640px+), `md:` (768px+), `lg:` (1024px+), `xl:` (1280px+), `2xl:` (1536px+).
* `p-2 sm:p-4 lg:p-6` — small padding on mobile, medium on tablet, large on desktop.
* NEVER assume desktop-first — `lg:p-6 md:p-4 sm:p-2` reads backward AND breaks Tailwind's
  cascade logic.

## Design tokens

    // tailwind.config.js — v3
    theme: {
      extend: {
        colors: { primary: '#0f766e', 'primary-fg': '#f0fdfa', muted: '#e5e5e5' },
        spacing: { '18': '4.5rem' },
        fontFamily: { display: ['Inter Display', 'sans-serif'] },
      },
    },

* Extend, don't replace. Overriding the whole colour palette drops the useful defaults (slate,
  zinc, red, green).
* Roles, not values: `primary` not `teal-700`. Re-branding stays localised.
* For shadcn/ui compatibility: use CSS variables (`--primary`, `--foreground`) referenced by
  the tailwind config. See the shadcn skill.

## Dark mode

* Default: `dark:` variant activates when `class="dark"` is on `<html>` or a parent.
* Configure in v3: `darkMode: 'class'` in `tailwind.config.js`.
* `bg-white dark:bg-neutral-950 text-neutral-900 dark:text-neutral-50` — always pair the light
  and dark form.
* System preference via `dark:` with `darkMode: 'media'` — but users can't override, so class-
  based is preferred.

## Variants

Order: `[responsive]:[dark]:[state]:utility`. E.g. `md:dark:hover:bg-primary`.

Common states:

* `hover:` — pointer hover.
* `focus:` `focus-visible:` — keyboard focus (`focus-visible` is what you want for a11y).
* `active:` — being clicked.
* `disabled:` — form control disabled.
* `group-hover:` `peer-hover:` — parent / sibling state.
* `data-[state=open]:` — data-attribute matching (used heavily by Radix / shadcn).

## Arbitrary values

`w-[calc(100vh-4rem)]` when a design token doesn't fit. Use SPARINGLY — every arbitrary value
is a token that ISN'T in your system.

Bad: `text-[#ff5733] p-[13px] mt-[27px]`.
Good: `text-danger p-3 mt-6` with those tokens defined in the theme.

## Composing components

When the same combination appears 3+ times, EXTRACT:

* React: `<Card className="rounded-lg border bg-card text-card-foreground shadow-sm">…</Card>`.
* Vue / Blade: same idea, template-level.
* Tailwind's `@apply` for CSS classes: fine sparingly, but the community consensus is that
  extracting COMPONENTS (React / Vue / Blade partials) is cleaner than extracting CSS classes.
  `@apply` breaks when a component's classes need to be dynamic.

## Anti-patterns

* Every element on a page is `bg-white p-4 rounded-lg shadow-md border`. This is a CARD; extract.
* `!important` sprinkled with `!` prefixes: `!p-8 !bg-red-500` — you're fighting the framework.
  Find the source of the override you're trying to beat.
* Hardcoded `text-black bg-white` — breaks dark mode. Use `text-foreground bg-background`.
* Arbitrary values for every measurement — the design system is broken.
* Full-line comment strings with 20+ utilities — refactor into a component.

## Performance

* JIT scans your source; the output CSS contains ONLY classes you use. Bundle is usually 5-50 KB
  gzipped for a whole app.
* If your CSS is 500 KB: your `content` glob is scanning `node_modules/` — narrow it.
* If your CSS is 0 KB and the page is unstyled: your `content` glob missed the template.

## Verification

* `npm run build` — succeeds.
* Load a representative page — classes render (not purged).
* Toggle dark mode — pairs work.
* Resize the window — breakpoints trigger.
* Reduced motion: does the page still work with animations disabled? Test with the DevTools
  Rendering panel → Emulate CSS media feature `prefers-reduced-motion: reduce`.
