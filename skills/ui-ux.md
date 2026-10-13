---
name: UI/UX Design
tags: [ui, ux, design, interface, product, visual, layout, typography]
dependencies: []
conflicts: []
version_hint: n/a (principles, not a version)
project_detect: []
summary: Design interfaces that look intentional rather than assembled. Rely on hierarchy, spacing, typography, restraint — not templates, gradients, glass, or badge collections. Every element earns its place. Empty / loading / error states are first-class layouts. Motion and colour serve meaning; they do not decorate.
---

# UI/UX Design (production quality)

The bar is: **someone who has shipped a real product should look at the interface and think it
was designed, not assembled**. Not a template. Not an "AI-generated dashboard". A genuine
product.

Almost everything below is subtractive. Most "designed" interfaces get worse when you add. The
few things that MAKE an interface designed — hierarchy, typography, restraint, purposeful motion —
are cheap; every replacement pattern that follows in this document is longer than the pattern it
replaces because most people default to the wrong one.

## Anti-patterns — DO NOT produce these

These are the "AI-generated interface" tells. If your composition includes any of them without a
specific product reason, remove or replace.

* **Rounded cards everywhere**. Cards are FRAMES; use them sparingly to isolate a thing that
  needs a boundary. A dashboard where every row and every panel is a rounded card looks like
  padding, not design.
* **Gradients as decoration**. Purple-to-blue gradients on hero sections, buttons, avatars, and
  section backgrounds — all at once. Gradients are a bold move; use one, deliberately, or none.
* **Glass / blur / neomorphism**. `backdrop-blur-md` on a card, on a nav, on a modal, on a
  toast — the interface starts looking like a Vision Pro tech demo, not a product. Reserve blur
  for actually-overlaid surfaces (a sticky nav over scrolling content, a modal scrim).
* **Meaningless glowing effects**. Colored shadows / halos on buttons. Halos read as brand IF
  used once (a hero CTA); they read as noise everywhere else.
* **Giant headings for their own sake**. `text-6xl` on a page whose actual content is a form of
  four inputs. The heading now dwarfs the work.
* **Excessive badges**. "NEW", "BETA", "PRO", "AI", "PREMIUM", "TRENDING" — six badges on one
  card, no hierarchy. A badge is a label of exception; use one at a time.
* **Repetitive cards in a grid**. Three-column card grid where every card has an icon, a title,
  a subtitle, a button — over eight rows. That's a table, not a card grid.
* **Generic dashboard layouts**. Left rail, top KPI row, main chart, right rail. This is a
  Bootstrap dashboard from 2014. Real dashboards are shaped by the DECISION the user is trying
  to make.
* **Arbitrary icons**. Icon next to every button, every menu item, every heading. Icons should
  either be systematic (all present, consistent metaphor) or absent (all absent).
* **Excessive shadow depth**. `shadow-2xl` on every card. Shadow is elevation; if everything is
  elevated, nothing is.
* **Huge whitespace with no compositional purpose**. Empty gaps between sections because "modern
  design uses whitespace". Whitespace is compositional — it separates GROUPS; it doesn't pad
  every element.
* **Sections that look identical**. Landing page with 5 sections, each an alternating light /
  dark stripe with an icon + title + paragraph + button. That's a template.
* **Template-looking landing pages**. If a critical viewer can tell it was assembled from a
  landing-page kit at first glance, it's a template.

## Principles that MAKE an interface look designed

### Hierarchy is the single biggest lever

There should be ONE most-important thing on any screen. Two, at most. Every other element sits
under it in a visible ranking. If everything is emphasised, nothing is.

Ways hierarchy is expressed (in order of strength):

1. Size (biggest is most important, usually — but not always: a giant CTA on a landing page vs.
   the tiny X close button on a modal both work).
2. Colour + contrast (the ONE brand element vs. everything neutral).
3. Weight (bold vs. regular).
4. Position (top-left, above the fold, first in reading order).
5. Whitespace (isolation reads as importance).
6. Motion (subtle, once — a single element that animates on load draws the eye).

A designed interface uses TWO or THREE of these on the primary element and ONE on the secondary.
An assembled interface uses all of them on everything.

### Typography carries the design

The typeface, the scale, the leading — this is 60% of what makes an interface feel professional.

* Pick a body typeface and a display typeface (they can be the same). System stack works for
  utilities; a real product invests in one licensed typeface (Inter / IBM Plex / Söhne / Untitled
  Sans / Neue Haas Grotesk — any of these before you invent).
* Build a scale, not a random-size collection. A modular scale (14, 16, 18, 20, 24, 30, 36, 48,
  60, 72) or Tailwind's default is fine. Every size used must belong to the scale.
* Line-height is often wrong in AI-generated UIs. Body text at 14-16px wants line-height ~1.5.
  Display text wants line-height ~1.05-1.15.
* Kerning: the default is fine for body; display text ≥ 30px looks better with a small negative
  letter-spacing (`tracking-tight` in Tailwind).
* Contrast: primary text at 14:1+ (near black on white); secondary at 5-7:1; disabled at 3-4:1.
  Never lower.

### Spacing on a system

Everything spaces on a rhythm. If you have `p-4 mt-6 gap-3 mb-8 p-2.5` on adjacent elements,
the spacing looks noisy. Pick a system (Tailwind's 4/8/12/16/24/32/48 scale is fine), and STICK
TO IT. Two consecutive sections that break the rhythm look intentional; five do not.

### Colour on a system

* Neutral palette (background, surface, muted, foreground, muted-foreground): 5-6 tones.
* Brand colour (ONE hue, in 2-4 tones).
* Semantic (success, warning, danger, info): 4 hues, one tone each.

That's it. If you find yourself reaching for a seventh colour, either promote it to the palette
(with intention) or find a way to express the meaning with hierarchy instead.

### Density serves the user

Enterprise / data-heavy interfaces want DENSITY. A CRM row is not a card. A trading table has
30 columns and 200 rows — every millimetre matters. Consumer apps want AIR. A meditation app has
one button on a screen. The extremes are honest.

Middle-of-the-road density (everything on generic-looking cards) is the AI-template tell.
Choose: dense-and-productive OR sparse-and-focused.

### Motion has meaning or nothing

* Cross-fade on route change: fine.
* Toast slide-in from top-right: fine.
* Modal fade + subtle scale on open: fine.
* EVERY element animating in on scroll, staggered by 0.05s, with a bouncy spring: not fine.

Motion COMMUNICATES: something changed, something needs attention, something is arriving or
leaving. Motion for decoration is jitter.

Respect `prefers-reduced-motion` — see the a11y skill.

## Empty / loading / error states are first-class layouts

Every screen has FOUR states, not one. If a designer only mocks "loaded with data", the other
three end up as `Loading…` centered text and blank pages. Real products design all four.

**Empty state**: what does the user see the first time, with no data? An empty CRM contact list
is not "0 contacts". It's an illustration + one-sentence explanation + a primary CTA
("Add your first contact"). It should teach WHAT to do next.

**Loading state**: skeletons that match the eventual layout (grey boxes at the sizes and
positions of the real cards/rows), NOT a centered spinner. Spinners lie about progress; skeletons
show shape.

**Error state**: a real message ("Couldn't reach the server. Retry?") with a retry action.
Never a stack trace to the user. Never "Something went wrong."

**Success/settled state**: what the user actually sees most of the time.

Design all four.

## Onboarding is the first hour, not the first minute

Common failure: a five-slide product tour on first launch, then dropped into an empty app. Users
skip the tour and stare at nothing.

Instead: **contextual onboarding**. First launch shows the empty state with a one-step "let's
add your first X" flow. Second visit shows a subtle hint on the feature that would help most.
Fifth visit shows nothing.

Progress disclosure. A user with 0 tasks doesn't see the analytics dashboard; a user with 200
tasks does.

## Navigation

* Consumer apps: 3-5 tabs; anything more is a menu. Icon + label pair, not icon-only unless the
  glyph is universally understood (Home, Search).
* Productivity apps: sidebar with collapsible sections; the user is here for a while and can
  afford density.
* Marketing sites: horizontal nav, 4-7 items, one primary CTA in the top-right, sign-in link on
  the far right.

If your app has 20 top-level features, you have a taxonomy problem, not a navigation problem.

## Dashboards

Bad dashboards: rows of "KPI cards" that all look the same, then a chart, then a table. Good
dashboards start from the question the user is trying to answer.

* Sales team: "how am I tracking to quota this quarter?" — big number, sparkline, list of the
  three deals most likely to close, list of the three at risk. That's it.
* DevOps: "is anything on fire?" — status grid, then a stream of the most recent errors, then
  drill-down. No KPI cards.
* Analytics: "what changed in the last 7 days?" — deltas prominent, absolute numbers secondary.

The KPI-card row is a design cop-out.

## Forms

* One column, top-labeled fields, tab order matches reading order. Deviate only for genuinely
  paired fields (First / Last, Card number / CVC / Expiry).
* Inline validation on blur (not on every keystroke — annoying) and on submit.
* Submit button lives where the eye lands last (bottom-right of the form on wide screens; full-
  width bottom on mobile).
* Long forms: chunk into steps. Users abandon 30-field single-page forms.
* Errors: near the field, one sentence, in red. Never "Please review the errors below" at the
  top with no in-field markers.

## Data-heavy interfaces

* Truncate LONG. `Truncate…` with a tooltip beats wrapping and disturbing row heights.
* Right-align numbers, left-align text. Always.
* Colour-code by category, not by "importance" (heat maps are the exception).
* Freeze the first column and the header row on scroll for tables > one screen wide.

## Mobile UX

* Touch targets ≥ 44×44pt (Apple HIG) / 48×48dp (Material). Yes, always.
* Bottom nav for primary actions (thumb reach). Top nav for navigation to secondary areas.
* Modals fill the sheet on mobile; overlay on desktop.
* No hover states as primary interaction — mobile has no hover.
* Test on the smallest device you support (iPhone SE 375pt wide).

## Desktop UX

* Keyboard: every action reachable. `⌘K` for command palette; `?` for shortcuts; `Esc` closes
  everything.
* Focus visible always. Never `outline: none` without a replacement.
* Text selection allowed on data. Users copy-paste.
* Right-click matters on data-heavy interfaces (custom context menus for the most-used actions).

## Dark and light modes

* Design light FIRST, then translate to dark. The reverse tends to over-glow.
* Not "invert everything" — dark mode reduces saturation on brand colours; muted-foreground
  moves from 45% opacity black to 65% opacity white (not just inversion).
* Contrast IN DARK MODE is where AI-generated UIs fail — `text-gray-400 bg-gray-900` looks fine
  in Figma and reads as illegible on a screen. Check every text-on-surface pair with a real
  contrast checker.

## Design tokens

* Colour, spacing, type-scale, radius, shadow — as CSS variables (or Tailwind theme extensions).
* Named by ROLE (`--color-surface`, `--color-primary`, `--radius-md`), never by VALUE
  (`--color-red-500`, `--radius-8`). Roles let you re-brand without touching component code.
* Two token layers: PRIMITIVES (all the raw scales) and SEMANTICS (`surface = neutral-50 light /
  neutral-900 dark`). Components use semantics; primitives are internal.

## Brand identity in a product

* A logo mark (SVG, monochrome), a signature colour (one hue, 2-4 tones), a signature typeface,
  a signature radius. Products like Stripe, Linear, Vercel, Arc — each is instantly recognisable
  from a screenshot. That comes from a tight, restrained brand system.
* Do NOT put the logo everywhere. It belongs in the top-left of the primary nav and on marketing
  surfaces. Not on every card, every empty state, every avatar fallback.

## Sanity checks before shipping

1. Show the screen to someone who's never seen it: they identify the primary action in <5 s?
2. Squint at it: the hierarchy is still readable? (Focus, not colour.)
3. Take a screenshot, view at 50% zoom: still recognisable? (Composition, not detail.)
4. Toggle dark mode: still balanced?
5. Tab through with the keyboard: focus visible, order sensible?
6. On a slow 3G throttled connection: what does the loading state look like for 2 seconds?
7. Run axe / accessibility checker: zero errors, zero contrast warnings?
8. Design at 3 breakpoints (mobile 375, tablet 768, desktop 1440); each holds together?

If any answer is no, the interface is not done. Do not ship on the strength of "the happy path
renders" — that's the assembly test, not the design test.
