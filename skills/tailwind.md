---
name: Tailwind CSS
tags:
  - tailwind
  - css
  - frontend
  - responsive
  - vite
---

# Tailwind CSS

## Setup
- Install: `npm install -D tailwindcss postcss autoprefixer && npx tailwindcss init -p`.
- Set `content` globs in `tailwind.config.js` to every template path that uses classes (e.g. `./resources/**/*.blade.php`, `./src/**/*.{js,ts,jsx,tsx}`) — missing globs = purged/blank styles.
- Add the three `@tailwind base; @tailwind components; @tailwind utilities;` directives to the main CSS entry.

## Responsive & layout
- Mobile-first: unprefixed utilities apply to all sizes; `sm:` `md:` `lg:` `xl:` layer up.
- Prefer flex/grid utilities (`flex`, `grid grid-cols-*`, `gap-*`) over manual CSS.
- Use the design tokens (spacing, colors) consistently; extend the theme in `tailwind.config.js` rather than hardcoding hex values inline.

## Verification
- The compiled CSS is produced by the build (`npm run build`) — a successful build is the evidence.
- Check that classes actually render (not purged) by loading a page; a blank/unstyled page usually means a wrong `content` glob.
