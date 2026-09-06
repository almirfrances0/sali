---
name: shadcn/ui
tags: [shadcn, shadcn-ui, radix, tailwind, react, nextjs, ui, components, design-system]
dependencies: [tailwind, react]
conflicts: []
version_hint: shadcn@latest (component distribution, not a versioned library)
project_detect:
  - components.json
  - components/ui
  - app/components/ui
summary: shadcn/ui is a component-DISTRIBUTION model, not a black-box component library. The CLI copies source into the project (usually under components/ui); the project owns and edits those files. Compose with Tailwind + Radix primitives; theme via CSS variables in globals.css; add components with `npx shadcn@latest add <name>`. Fits Next.js App Router, plain React (Vite), Astro, and Laravel + Inertia projects with a JS bundler. Prefer official components over hand-rolled ones; extend by editing the copied source, not by wrapping.
---

# shadcn/ui

Radix primitives + Tailwind, distributed as source code. You install a component and it lands as
a real `.tsx` file in your repo — you own it, you edit it, you commit it. There is no npm package
called "shadcn/ui" whose components you `import` at runtime; there is a CLI that copies files.
Every consequence of that model — good and bad — flows from that fact.

## When to reach for shadcn

Use shadcn when the project needs a **coherent, accessible, themeable** set of components AND the
team is willing to own the source. Do not reach for shadcn when the app is one screen and a
handful of buttons — you'll fight the setup for no leverage. Do not reach for shadcn in a
component library you're SHIPPING to third parties — you'd be re-exporting components those
consumers should be adding themselves.

Common wrong instincts to avoid:

* "Just import Button from shadcn/ui" — no such import. Add the component first.
* "I'll wrap Button to add my brand" — usually wrong; edit `components/ui/button.tsx` directly.
* "Bring in the whole library" — the whole point is picking components; adding everything defeats
  the design.
* "Update shadcn to fix a bug" — there's nothing to update. Edit the file. Optionally re-diff
  with `npx shadcn@latest diff <name>` if a newer upstream form matters.

## Setup

1. Initialise a Tailwind project first (Tailwind 3.x with `content` globs, or Tailwind 4 with the
   new `@import "tailwindcss"` entry). Verify the build produces styles before adding components.
2. `npx shadcn@latest init` — answers a project questionnaire and writes `components.json` (the
   canonical config), `lib/utils.ts` (with the `cn` helper on top of `clsx` + `tailwind-merge`),
   and theme variables into `app/globals.css`. If the CLI can't detect Next / Vite / Astro, pick
   the framework explicitly.
3. Confirm `components.json` matches your source layout — the `aliases.components` and
   `aliases.utils` paths MUST be resolvable in `tsconfig.json` (`paths: { "@/*": ["./*"] }`).
   Wrong aliases = every added component is a broken import.
4. Add one component to sanity-check: `npx shadcn@latest add button`. It appears at
   `components/ui/button.tsx`. Import it in a page. Verify the styles apply (a purple button with
   no styling means Tailwind's `content` glob doesn't include `components/**`).
5. Commit `components.json`, `lib/utils.ts`, `app/globals.css`, and every file the CLI wrote.

## Adding + composing components

* Add components with `npx shadcn@latest add <name>` — never copy-paste from the docs by hand,
  because you'll drift from the CLI's dependency resolution (`button` requires `@radix-ui/react-slot`,
  `dialog` requires `@radix-ui/react-dialog`, etc.).
* Compose complex UI from small primitives, not the other way around. A "combobox" is `Popover` +
  `Command` + `Button`. Assemble.
* When the same wrapper appears three times, extract it to `components/YourThing.tsx` — but keep
  `components/ui/*` for shadcn primitives so a re-init or diff still knows what it owns.

## Theming

Themes live in CSS variables, not Tailwind config. `app/globals.css` sets `--background`,
`--foreground`, `--primary`, `--muted`, `--ring`, etc. per light + dark mode. Tailwind classes
like `bg-primary text-primary-foreground` resolve those variables via the shadcn preset (v3) or
the `@theme` block (v4). To brand:

* Change the CSS variables — never the Tailwind class names. `bg-primary` should mean "the
  brand's primary". If you rename it to `bg-brand-blue`, you lose the whole compositional model.
* For dark mode, prefer the `class="dark"` strategy on `<html>` (shadcn's default). Toggle via
  `next-themes` in Next.js, plain React state elsewhere.
* Contrast the pair `bg-primary` + `text-primary-foreground` — both variables move together. If
  you change the primary hue, adjust the foreground to keep 4.5:1 contrast (see the a11y skill).

## Forms

* Use `react-hook-form` + `zod` (both are what the shadcn Form docs assume). `@hookform/resolvers`
  bridges them. `zod` schema → `useForm({ resolver: zodResolver(schema) })` → `<FormField
  control={form.control} name="…" render={({ field }) => <Input {...field} />} />`.
* The shadcn `Form` component set is a light wrapper over `react-hook-form`'s `FormProvider`.
  Field errors surface via `FormMessage` — do not roll your own error text. Screen readers get
  the `aria-invalid` + `aria-describedby` linkage for free.
* Server-side validation still applies. `zod` schemas can live in `lib/schemas.ts` and be shared
  between client (react-hook-form) and server (Server Action / API route).

## Dialogs, sheets, dropdowns

* `Dialog`, `AlertDialog`, `Sheet`, `Drawer` are the four "overlay" primitives — pick by intent:
  Dialog = modal task, AlertDialog = irreversible confirmation, Sheet = side panel, Drawer = pull
  from an edge on mobile.
* All are Radix under the hood — focus is trapped, `Esc` closes, `aria-modal` set. If focus goes
  somewhere weird, the culprit is almost always a component OUTSIDE the primitive stealing focus
  in a `useEffect`.
* Never render a dialog conditionally at a high tree level with `open={condition && <Dialog…/>}` —
  that unmounts the dialog on close and loses the exit animation. Render it always; use `open` +
  `onOpenChange` to control visibility.

## Data displays

* `Table` from shadcn is a styled semantic `<table>`. For sortable / filterable data, compose with
  TanStack Table (`@tanstack/react-table`) — shadcn's Table example doc shows the pattern.
* For dense tabular data with 10 000+ rows, use virtualization (`@tanstack/react-virtual`) — the
  shadcn wrapper does NOT virtualise for you.
* For empty / loading / error states, treat them as first-class layouts, not afterthoughts. See
  the `ui-ux` skill's "Empty states" section — every table needs one.

## Command palette / search

* `Command` (built on `cmdk`) is what "⌘K" search bars are made of. Compose with `Dialog` to open
  it globally (see the docs' "Command Menu" example). Register items dynamically from your app's
  routes / actions.
* Keep the palette tight: 20-40 items visible before search. If you have hundreds, group with
  `CommandGroup` and lazy-load categories on first search.

## Icons

shadcn assumes `lucide-react` (imported per-icon so the bundle stays small). Prefer lucide over
importing a whole icon library. When lucide lacks a glyph, use `@radix-ui/react-icons` or inline
an SVG under `components/icons/`.

## AI-agent skills (shadcn's own registry model)

shadcn's newer registry model allows publishing components + AI-agent skills together. When you
have components stable enough to reuse across projects, publish a private registry
(`components.json` `registries` field) and consume with `npx shadcn@latest add @myreg/button`. An
agent skill file bundled with the registry teaches the LLM how to compose the component correctly
— the shape shadcn recommends is aligned with what this skill file itself is.

## Framework-specific notes

**Next.js (App Router)**: shadcn defaults assume Server Components — Radix primitives that use
state (`Dialog`, `Popover`, `DropdownMenu`) must be `"use client"` at their top. The shadcn CLI
adds this automatically; keep it. Do not add `"use client"` at page level unnecessarily.

**Vite + plain React**: works the same; `components.json` picks the Vite framework. No SSR
concerns. Bundle stays lean because each component is added on-demand.

**Laravel + Inertia + React**: works if you're bundling with Vite. `components/ui/*` lives under
`resources/js/components/ui/`. Ensure Tailwind's `content` globs cover `resources/**/*.tsx`.

**Astro**: works; components are React islands. Use `client:load` sparingly — most shadcn
components are interactive and need hydration, but static Cards / Badges can render pure SSG.

## Accessibility

Radix does most of the heavy lifting. Rules that STILL apply:

* Every interactive component needs a visible focus ring. shadcn's default `focus-visible:ring-2`
  is on by default — don't strip it.
* `aria-label` on icon-only buttons (`<Button size="icon"><Trash /></Button>` needs
  `<Button size="icon" aria-label="Delete">`). The linter won't tell you.
* Contrast for `bg-muted text-muted-foreground` at the default theme is ~7:1. If you re-brand
  toward pastels, re-check with an accessibility contrast checker; the muted pair is easy to break.
* `Dialog` inside a portal — screen readers see it; test with VoiceOver / TalkBack once.

## Anti-patterns

* **Wrapping every primitive** ("MyButton", "MyCard", "MyDialog"). You now maintain a private
  design system that reimplements shadcn poorly. Edit `components/ui/*` and move on.
* **Overriding via `className` for structural changes**. `className="w-full"` is fine.
  `className="!bg-red-500 !text-white !p-8 !rounded-none"` means you should be editing the file.
* **Fighting the theme**. `text-black bg-white` hardcodes light mode. Use the CSS-variable pair
  (`text-foreground bg-background`) and dark mode works for free.
* **Rendering `Dialog` conditionally**. `{open && <Dialog…/>}` breaks animations and focus
  restore. Render always, control with `open`.
* **Using every component "for consistency"**. Empty states with a giant `Card` + `Badge` +
  `Button` in a `Skeleton` frame is not consistent — it's clutter. See ui-ux/anti-patterns.

## Verification

* Every component renders when JS is disabled? Server-only pages should — client-only overlays
  won't; that's expected.
* Tab through every interactive component with the keyboard: focus visible, order sensible.
* Toggle dark mode: contrast holds; no hardcoded colours flash white.
* `axe` browser extension on a representative page: goal is zero errors, zero contrast warnings.
* Bundle size: `npm run build` should not add more than ~5-15 KB (gzipped) per component beyond
  the Radix primitive it uses. If a Card adds 200 KB, an icon library was pulled in whole.
