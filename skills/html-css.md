---
name: HTML & CSS
tags: [html, css, javascript, website, web, frontend, vanilla, portfolio, landing, page, responsive, dark-theme, animation]
dependencies: [ui-ux]
version_hint: HTML5 + CSS3 + vanilla ES2024
project_detect:
  - index.html
  - style.css
  - main.js
summary: Build multi-page websites with vanilla HTML, CSS, and JavaScript. CSS variables for theming. Mobile-first responsive design. IntersectionObserver for scroll animations. No frameworks. Every class used in HTML MUST be defined in CSS — define styles first, then write HTML that references them. File structure: project-folder/css/style.css + js/main.js + *.html at root.
---

# HTML & CSS (vanilla, production quality)

## File Structure

Every multi-page website lives in its own folder. NEVER scatter files at the workspace root.

```
project-name/
  index.html          # landing page
  about.html          # about page
  services.html       # services page
  contact.html        # contact page
  css/
    style.css         # ALL styles in one file
  js/
    main.js           # ALL scripts in one file
  images/             # if needed
```

- ONE CSS file. ONE JS file. Shared across all pages via `<link>` and `<script>`.
- NEVER create duplicate files at the workspace root AND in the project folder.
- NEVER use inline `style=""` attributes. ALL styling goes in the CSS file.

## Writing Order (CRITICAL for consistency)

**Write CSS FIRST, then HTML pages, then JS last.**

Why: when you write CSS first, the class names are fresh in your context when you write each HTML page. If you write HTML first, you invent class names that don't exist in the CSS. The model's context window is limited — CSS written in turn 1 is forgotten by turn 2.

Before writing ANY HTML page, READ the CSS file to see what classes are available. Only use class names you can see in the CSS.

## CSS Architecture

### Variables (always first in the file)

```css
:root {
  /* Colors */
  --bg-primary: #0a0e17;
  --bg-secondary: #111827;
  --bg-card: #1a2234;
  --text-primary: #f1f5f9;
  --text-secondary: #94a3b8;
  --text-muted: #64748b;
  --accent: #3b82f6;
  --accent-secondary: #8b5cf6;
  --border: rgba(148, 163, 184, 0.1);
  
  /* Spacing */
  --space-xs: 8px;
  --space-sm: 16px;
  --space-md: 24px;
  --space-lg: 40px;
  --space-xl: 64px;
  --space-2xl: 100px;
  
  /* Typography */
  --font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  --font-size-sm: 0.875rem;
  --font-size-base: 1rem;
  --font-size-lg: 1.125rem;
  --font-size-xl: 1.5rem;
  --font-size-2xl: 2rem;
  --font-size-3xl: 2.75rem;
  
  /* Borders */
  --radius-sm: 8px;
  --radius-md: 12px;
  --radius-lg: 20px;
  
  /* Transitions */
  --transition-fast: 0.2s ease;
  --transition-normal: 0.3s ease;
}
```

### Reset (always after variables)

```css
*, *::before, *::after {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}
html { scroll-behavior: smooth; }
body {
  font-family: var(--font-family);
  background: var(--bg-primary);
  color: var(--text-primary);
  line-height: 1.6;
  overflow-x: hidden;
}
a { text-decoration: none; color: inherit; }
ul { list-style: none; }
img { max-width: 100%; display: block; }
```

### Layout Classes (define once, use everywhere)

```css
.container {
  width: 100%;
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 var(--space-md);
}
.section-padding { padding: var(--space-2xl) 0; }
```

### Component Pattern

Every component follows this pattern:
```css
/* Component name */
.component { /* base styles */ }
.component-element { /* child styles */ }
.component--modifier { /* variant */ }
```

### Dark Theme

Use CSS variables for ALL colors. Never hardcode hex values in component styles. The `:root` block IS the theme — changing variables changes everything.

### Responsive Design (mobile-first)

```css
/* Base: mobile (320px+) */
.grid { display: grid; gap: var(--space-md); }

/* Tablet (768px+) */
@media (min-width: 768px) {
  .grid { grid-template-columns: repeat(2, 1fr); gap: var(--space-lg); }
}

/* Desktop (1024px+) */
@media (min-width: 1024px) {
  .grid { grid-template-columns: repeat(3, 1fr); gap: var(--space-xl); }
}
```

NEVER assume desktop-first. ALWAYS start with mobile styles and layer up.

### Required Classes for Multi-Page Sites

Every multi-page site MUST define these classes in the CSS:

**Navigation:**
- `.navbar` — fixed top nav
- `.nav-content` — flex container inside nav
- `.nav-links` — the `<ul>` of links
- `.nav-link` — each `<a>` in the nav
- `.mobile-toggle` — hamburger button for mobile
- `.logo` — site logo/name

**Page structure:**
- `.container` — max-width wrapper
- `.section-padding` — vertical padding for sections
- `.section-header` — section title + subtitle wrapper
- `.section-title` — `<h2>` in section headers
- `.section-desc` — `<p>` in section headers
- `.page-header` — top banner for inner pages (About, Services, Contact)

**Hero:**
- `.hero` — hero section
- `.gradient-text` — accent gradient text effect

**Buttons:**
- `.btn` — base button
- `.btn-primary` — filled accent button
- `.btn-outline` — bordered button

**Cards:**
- `.card` — generic card component

**Footer:**
- `.footer` — footer section
- `.footer-grid` — footer column layout
- `.footer-brand` — brand column
- `.footer-links` — link columns
- `.footer-bottom` — copyright bar

**Animations:**
- `.fade-up` — fade in from below (initial state)
- `.fade-up.visible` — final state after IntersectionObserver triggers
- `.delay-1`, `.delay-2`, `.delay-3` — staggered animation delays

**CTA:**
- `.cta` — call-to-action section
- `.cta-content` — CTA text wrapper

## JavaScript Pattern

```javascript
document.addEventListener('DOMContentLoaded', () => {
  // 1. Navbar scroll effect
  // 2. Mobile menu toggle
  // 3. Scroll animations (IntersectionObserver)
  // 4. Form validation (if contact page)
  // 5. Counter animations (if stats section)
});
```

### Scroll Animation (always use IntersectionObserver, never scroll events)

```javascript
const observer = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      entry.target.classList.add('visible');
      observer.unobserve(entry.target);
    }
  });
}, { threshold: 0.1 });

document.querySelectorAll('.fade-up').forEach(el => observer.observe(el));
```

### CSS for fade-up animation

```css
.fade-up {
  opacity: 0;
  transform: translateY(30px);
  transition: opacity 0.6s ease, transform 0.6s ease;
}
.fade-up.visible {
  opacity: 1;
  transform: translateY(0);
}
.delay-1 { transition-delay: 0.1s; }
.delay-2 { transition-delay: 0.2s; }
.delay-3 { transition-delay: 0.3s; }
```

## Anti-patterns — DO NOT produce these

- **Inline styles**: NEVER use `style="..."` on HTML elements. ALL styling goes in the CSS file.
- **Class names without CSS definitions**: every `class="X"` in HTML MUST have a corresponding `.X` in the CSS.
- **Different class names across pages**: if index.html uses `.nav-content`, about.html MUST use `.nav-content` too — not `.nav-container`.
- **Duplicate files**: NEVER create the same file at both the workspace root and a subfolder.
- **CSS without responsive breakpoints**: every layout MUST work on mobile (320px) through desktop (1920px).
- **Hardcoded colors**: use CSS variables, never raw hex values in component styles.
- **Google Fonts without fallback**: always include system font fallbacks.
- **Empty CSS classes**: don't define classes that aren't used.

## Checklist Before Declaring a Website Done

1. Every HTML page has `<link rel="stylesheet" href="css/style.css">`
2. Every HTML page has `<script src="js/main.js"></script>`
3. Every class in HTML exists in CSS (run: `grep -oP 'class="([^"]*)"' *.html | ...`)
4. Navigation is identical across all pages (same classes, same structure)
5. Footer is identical across all pages
6. All pages are responsive (test at 375px, 768px, 1024px widths)
7. Scroll animations work on all pages
8. Contact form validates before submission
9. No inline styles anywhere
10. No duplicate files at workspace root
