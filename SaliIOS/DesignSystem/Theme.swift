import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

/// The visual system (§37). One source of truth for spacing, radii, typography, motion, and semantic colors.
///
/// Identity: **pure ink** — a disciplined monochrome (black · white · grays). The "accent" is not a hue; it
/// is adaptive ink (near-black in light, near-white in dark), the way Linear/Vercel read. Meaning is carried
/// by weight, shape, and spacing rather than saturated color, so the app stays calm and premium. A few muted
/// status tones survive for genuine signal (error/success/warning), used sparingly and never decoratively.
///
/// Four rules hold the system together. Every screen should be expressible with only what is below:
///
/// 1. **Type carries hierarchy, not color.** A monochrome app has no hue to lean on, so the type ramp has to
///    do the work colour normally does. Each role below is a *different size AND a different weight* — see
///    `Typography`.
/// 2. **Elevation always moves toward the light.** Higher = lighter, in BOTH schemes — see `Colors`.
/// 3. **One tempo.** Every animation in the app is one of a handful of tokens, and every ambient loop is a
///    whole multiple of a single beat — see `Motion`.
/// 4. **Two spacing registers.** Micro values shape a component's inside; macro values give a screen its
///    rhythm — see `Spacing`.
public enum Theme {

    // MARK: - Spacing

    /// Two registers, deliberately. The MICRO steps (`xs`…`l`) shape the inside of a component — the gap
    /// between a title and its subtitle, a chip's padding. The MACRO steps are the large vertical intervals
    /// *between* things, and they are what actually gives a screen its rhythm; without them every screen
    /// collapses into one evenly-spaced grey mass, because 16pt is simultaneously "these two lines belong
    /// together" and "these two sections are unrelated", which cannot both be true.
    ///
    /// Rule of thumb: if the value separates things a user perceives as *the same object*, it is micro. If it
    /// separates things they perceive as *different objects*, it is macro.
    public enum Spacing {
        // ── Micro: inside a component ───────────────────────────────────────────────────────────────
        /// Hairline gap — an icon and its label, a pill's vertical padding.
        public static let xs: CGFloat = 4
        /// Two lines of the same thought — a title and its subtitle.
        public static let s: CGFloat = 8
        /// A component's internal padding; the gap between sibling rows in a stack.
        public static let m: CGFloat = 12
        /// A card's padding; the standard horizontal screen margin.
        public static let l: CGFloat = 16

        // ── Macro: between things ───────────────────────────────────────────────────────────────────
        /// Between two blocks inside one section (a header's group and the note beneath it).
        public static let xl: CGFloat = 24
        /// Between two sections of a screen. The default "new topic" interval.
        public static let xxl: CGFloat = 32
        /// Between major regions of a long screen — where a user would expect a visual chapter break.
        public static let xxxl: CGFloat = 48
        /// The largest interval in the app: the air around a screen-opening statement or a full-screen
        /// empty state. Used at most once per screen; more than that and it stops meaning anything.
        public static let huge: CGFloat = 64

        // ── Semantic aliases ────────────────────────────────────────────────────────────────────────
        // The same numbers, named for the job. Prefer these at call sites: `Spacing.sectionGap` survives a
        // future retune of the ramp, `Spacing.xxl` only tells the next reader "32".
        /// Between stacked lines inside one row.
        public static let rowGap: CGFloat = s
        /// Between sibling cards in a list or grid.
        public static let itemGap: CGFloat = m
        /// Between blocks inside one section.
        public static let blockGap: CGFloat = xl
        /// Between two sections of a screen.
        public static let sectionGap: CGFloat = xxl
        /// Between major regions of a long screen.
        public static let chapterGap: CGFloat = xxxl
        /// Around a screen-opening statement or empty state.
        public static let heroGap: CGFloat = huge
        /// The horizontal margin of a free-form screen (`ScrollView` content, cards).
        public static let screenMargin: CGFloat = l
        /// The horizontal inset iOS uses for inset-grouped `List` content. `SectionHeader`/`SectionFooter`
        /// match it exactly so a header sitting in a `List` lines up with the row text beneath it.
        public static let listMargin: CGFloat = 20
    }

    // MARK: - Radius

    /// The only corner radii in the app. Raw literals drift screen to screen; these do not.
    ///
    /// One ramp, five steps, chosen so that *size implies radius*: the bigger the surface, the softer the
    /// corner. Mixing curvatures inside one screen is the fastest way to make an app look assembled rather
    /// than designed, so pick the step that matches the element's size and never interpolate between them.
    public enum Radius {
        /// Hairline geometry — quote rules, progress grooves, 3–4pt bars. Barely a curve, just not a corner.
        public static let xxs: CGFloat = 2
        /// Skeleton bars, tiny swatches, inline code chips.
        public static let xs: CGFloat = 4
        /// Fields and small controls — text fields, small buttons, segmented controls.
        public static let s: CGFloat = 8
        /// Cards and rows — the workhorse. Anything the size of a list row.
        public static let m: CGFloat = 12
        /// Banners, message bubbles, large containers.
        public static let l: CGFloat = 18
        /// Sheets, hero containers, anything spanning most of the screen.
        public static let xl: CGFloat = 22
        /// Capsules — pills, badges, tracks.
        public static let pill: CGFloat = 999

        /// Concentric radii. A shape inset by `inset` inside a container of `radius` must use
        /// `radius - inset`, or the two curves visibly fight along the diagonal — the single most common
        /// reason nested cards look subtly wrong. Never returns something sharper than `xxs`.
        public static func nested(in radius: CGFloat, inset: CGFloat) -> CGFloat {
            max(xxs, radius - inset)
        }
    }

    // MARK: - Stroke

    /// The one border treatment. A monochrome app separates surfaces with *edges*, not shadows, so the
    /// hairline is load-bearing here in a way it is not in a colourful app — which is exactly why it must be
    /// one width and one colour everywhere. Pair with `Colors.separator` (quiet) or `Colors.border`
    /// (a real container edge); never with raw `.gray`.
    public enum Stroke {
        /// Every container edge: cards, bubbles, fields, chips. One point, at every scale.
        public static let hairline: CGFloat = 1
        /// Emphasis only — a focused field, a selected chip. Anything thicker reads as a mistake.
        public static let emphasis: CGFloat = 1.5
    }

    // MARK: - Colors

    public enum Colors {
        // MARK: Elevation ramp (adaptive light / dark)
        //
        // THE RULE: **elevation always moves toward the light.** Higher in the stack = lighter, in BOTH
        // schemes. Nothing else is coherent — the previous ramp made "raised" *darker* than `surface` in
        // light and *lighter* in dark, so a card that read as lifted in dark read as a dent in light and
        // "raised" was not a concept the eye could learn.
        //
        // The consequence, and it is the right one: the light canvas is a soft off-white, not paper. That is
        // what buys light mode a top step — a raised card can be pure #FFFFFF against it, exactly the way
        // iOS inset-grouped tables work. A pure-white canvas has no room above it, which is what forced the
        // inversion in the first place.
        //
        //   sunken  ·  background  ·  surface  ·  raised     ← lighter →   in light AND dark
        //
        /// Recessed below the canvas: input wells, groove tracks, an inline code chip inside a raised card.
        /// The only step that goes *down*, and it goes down in both schemes.
        public static let surfaceSunken = dyn(0xEBEBEF, 0x030304)
        /// The canvas. A soft off-white in light so that everything above it has somewhere to go.
        public static let background   = dyn(0xF2F2F5, 0x0B0B0D)
        /// One step up: grouped list rows, the composer bar, section backgrounds.
        public static let surface      = dyn(0xF9F9FB, 0x161619)
        /// The top of the stack: cards, assistant bubbles, fields, sheets. Pure paper in light.
        public static let surfaceRaised = dyn(0xFFFFFF, 0x212125)

        // MARK: Ink tiers
        //
        // Three tiers, because two forced every timestamp, count, and unit label to borrow `secondaryText`
        // and shout as loudly as the sentence it annotates. Choose by *what the text is*, not by how it
        // looks: statement → primary, supporting sentence → secondary, machine metadata → tertiary.
        /// Statements: titles, values, the sentence the user came to read.
        public static let primaryText  = dyn(0x0B0B0C, 0xF5F5F6)
        /// Support: subtitles, descriptions, helper text. Still meant to be read.
        public static let secondaryText = dyn(0x6E6E76, 0x9A9AA2)
        /// Metadata: timestamps, counts, units, ids — present, glanceable, never competing. Pair with
        /// `Typography.metadata`.
        ///
        /// Quiet, but not *illegibly* quiet: both values clear 3.4:1 against every step of the elevation
        /// ramp in their scheme, which is the floor for 11pt medium. A tertiary tier that fails contrast is
        /// worse than no tertiary tier, because authors then use it for things that matter.
        public static let tertiaryText = dyn(0x82828C, 0x77777F)

        // MARK: Edges
        /// The quiet hairline: list separators, dividers, an edge inside an already-bounded surface.
        public static let separator    = dyn(0xE3E3E8, 0x2C2C32)
        /// A real container edge — a card or bubble sitting directly on the canvas needs a touch more
        /// definition than a divider inside it, especially now that raised is pure white in light.
        public static let border       = dyn(0xE0E0E6, 0x323238)
        /// Emphasis edge: focused field, selected chip. Use with `Stroke.emphasis`.
        public static let borderStrong = dyn(0xC9C9D2, 0x43434B)

        // MARK: Ink accent (adaptive) — the single interactive identity
        /// Near-black in light, near-white in dark. Used for primary fills, links, and active state.
        public static let accent       = dyn(0x0B0B0C, 0xF5F5F6)
        /// The legible foreground ON top of `accent` (paper vs ink). Always pair with `accent` fills.
        public static let onAccent     = dyn(0xFFFFFF, 0x0A0A0B)
        /// A whisper of ink for soft fills (chips, proactive bubbles, tracks).
        public static let accentSoft   = accent.opacity(0.10)

        // MARK: Placeholder
        /// The fill for content that isn't there yet — skeleton bars, empty avatars. A dedicated token
        /// because it must contrast with whatever it sits on, and `surfaceRaised` no longer can: on a raised
        /// card in light mode, raised-on-raised is white-on-white and the skeleton disappears.
        public static let placeholder  = dyn(0xE6E6EB, 0x2A2A30)

        // MARK: Muted status — real signal only, never decoration. Legible in both modes.
        public static let ok      = dyn(0x2F8F5B, 0x53B683)
        public static let warn    = dyn(0x9A7218, 0xD6A64E)
        public static let danger  = dyn(0xB8443B, 0xE86B5F)
        /// Informational == ink (kept as an alias so existing call sites stay monochrome).
        public static let info    = accent
        public static let idle    = dyn(0x9A9AA2, 0x6E6E76)

        // MARK: Adaptive color helper
        /// Build a color that resolves per light/dark trait from two 24-bit RGB hexes. Falls back to a
        /// fixed color where UIKit is unavailable (previews on non-iOS).
        ///
        /// Every colour in this file goes through here on purpose: a token defined for only one scheme is a
        /// bug that ships, because it looks fine in whichever scheme the author had open.
        static func dyn(_ light: UInt32, _ dark: UInt32) -> Color {
            #if canImport(UIKit)
            return Color(uiColor: UIColor { trait in
                trait.userInterfaceStyle == .dark ? UIColor(rgb: dark) : UIColor(rgb: light)
            })
            #else
            return Color(rgb: light)
            #endif
        }
    }

    /// The elevation ramp as a value, for anything that needs to *pass a level around* rather than name a
    /// colour — a reusable card that renders differently depending on what it sits on, say.
    public enum Elevation: Int, CaseIterable, Sendable {
        case sunken = -1, canvas = 0, surface = 1, raised = 2

        public var color: Color {
            switch self {
            case .sunken:  Colors.surfaceSunken
            case .canvas:  Colors.background
            case .surface: Colors.surface
            case .raised:  Colors.surfaceRaised
            }
        }
        /// One step up from here — for a chip that must stay visible on whatever it lands on.
        public var above: Elevation { Elevation(rawValue: min(rawValue + 1, 2)) ?? .raised }
    }

    // MARK: - Motion

    /// ONE tempo for the whole app.
    ///
    /// Motion used to be hand-rolled at every site — six independent `repeatForever` loops at 1.25s, 1.4s,
    /// 2.0s, 6.0s and so on. Two of those on screen together *beat* against each other: they drift in and
    /// out of phase, and the eye reads the interference as nervousness, which is the opposite of what an
    /// always-on assistant should feel like. So there are exactly three families here and nothing else:
    ///
    /// - **Discrete feedback** (`instant`, `quick`) — something happened *because the user did it*. Short
    ///   and eased-out so it lands with the finger.
    /// - **State transition** (`standard`, `gentle`, `deliberate`) — the screen is becoming a different
    ///   screen. Springs for anything with geometry, eases for anything that only changes opacity.
    /// - **Ambient** (`breathing`, `pulse`, `sweep`, `orbit`) — nothing happened; Sali is simply alive.
    ///   Every one of these is a whole multiple of `beat`, so two of them on screen stay in phase forever.
    ///
    /// ### Honoring Reduce Motion — the pattern
    ///
    /// Every token here MUST be consumed through `Motion.honoring(_:_:)`, which returns `nil` (SwiftUI's
    /// "no animation") when the user has asked for stillness:
    ///
    /// ```swift
    /// @Environment(\.accessibilityReduceMotion) private var reduceMotion
    ///
    /// // Discrete + state: gate the animation, keep the state change.
    /// .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.standard), value: isExpanded)
    /// withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick)) { isOn.toggle() }
    /// ```
    ///
    /// (Spell the token out — `Animation` has no `.quick`, so leading-dot inference cannot reach these.)
    ///
    /// Ambient loops need one extra step. A `repeatForever` animation gated to `nil` still *sets the driving
    /// state*, so the view silently jumps to the end pose and stays there. Never start the loop at all:
    ///
    /// ```swift
    /// .onAppear {
    ///     guard !reduceMotion else { return }              // ← do not set `phase` either
    ///     withAnimation(Theme.Motion.breathing) { phase = 1 }
    /// }
    /// ```
    ///
    /// And a moving *decoration* (a shimmer, a sweep) should not merely freeze — it should not be in the
    /// view tree: `.overlay { if !reduceMotion { sweep } }`.
    public enum Motion {

        // ── Discrete feedback ───────────────────────────────────────────────────────────────────────
        /// Under the threshold of feeling animated: a press scale, a checkmark. Use when the user must not
        /// perceive a delay at all.
        public static let instant  = Animation.easeOut(duration: Duration.instant)
        /// The default response to a tap: a chip selecting, a disclosure flipping, a button settling.
        public static let quick    = Animation.easeOut(duration: Duration.quick)

        // ── State transition ────────────────────────────────────────────────────────────────────────
        /// The workhorse. Anything with geometry — a row appearing, a card resizing, a sheet's content
        /// settling. A spring because real things have mass; damping 0.88 means it arrives without wobble.
        public static let standard = Animation.spring(response: 0.34, dampingFraction: 0.88)
        /// Opacity- and colour-only changes, where a spring has nothing to spring. Also the right choice for
        /// anything large enough that a spring would feel busy.
        public static let gentle   = Animation.easeInOut(duration: Duration.gentle)
        /// A whole screen region rearranging, or a change the user should *notice* rather than just accept.
        /// Slower and heavier damped — nothing at this scale should overshoot.
        public static let deliberate = Animation.spring(response: 0.55, dampingFraction: 0.92)

        // ── Ambient ─────────────────────────────────────────────────────────────────────────────────
        /// THE BEAT — one breath, in seconds. Every looping animation in the app is this number or a whole
        /// multiple of it. That is the entire reason ambient motion here reads as one calm system rather
        /// than several restless ones: harmonically related loops re-align instead of drifting.
        ///
        /// 1.6s is deliberately just below a resting human breath (~4s per full cycle, so ~2s per half). It
        /// reads as alive and unhurried; anything faster reads as loading, anything slower as stalled.
        public static let beat: Double = 1.6

        /// The core "thinking / streaming" motion: a slow swell and release. `autoreverses`, so one full
        /// cycle is `beat * 2`.
        public static let breathing = Animation.easeInOut(duration: beat).repeatForever(autoreverses: true)
        /// Half a beat — a typing dot, a live indicator. Faster than `breathing` but still harmonic with it,
        /// so the two never visibly interfere. Stagger siblings with `stagger(_:)`, never with a new duration.
        public static let pulse     = Animation.easeInOut(duration: beat / 2).repeatForever(autoreverses: true)
        /// One linear pass across a shape: a skeleton shimmer, an indeterminate progress track. Linear
        /// because an eased sweep looks like it is stuck at the edges.
        public static let sweep     = Animation.linear(duration: beat).repeatForever(autoreverses: false)
        /// Four beats — one full slow rotation, for the Sali mark's ring. Long enough that the eye reads it
        /// as presence rather than as a spinner promising imminent completion.
        public static let orbit     = Animation.linear(duration: beat * 4).repeatForever(autoreverses: false)

        // ── Raw durations ───────────────────────────────────────────────────────────────────────────
        /// The same tempo as plain seconds, for the places an `Animation` can't reach: `Task.sleep`,
        /// `.transaction`, a manual `withAnimation` delay, a `.delay(_:)` on a token.
        public enum Duration {
            public static let instant: Double = 0.12
            public static let quick:   Double = 0.18
            public static let standard: Double = 0.28
            public static let gentle:  Double = 0.40
            public static let slow:    Double = 0.60
            /// One ambient beat — see `Motion.beat`.
            public static let beat:    Double = Motion.beat
        }

        // ── Helpers ─────────────────────────────────────────────────────────────────────────────────

        /// **The only supported way to consume a motion token.** Returns `nil` — SwiftUI's "do it with no
        /// animation" — when Reduce Motion is on, so the state change still happens instantly and correctly.
        /// Both `withAnimation(_:)` and `.animation(_:value:)` take an `Animation?`, so this drops straight in.
        ///
        /// For ambient (`repeatForever`) tokens this is *not enough on its own* — see the type's doc comment.
        public static func honoring(_ reduceMotion: Bool, _ animation: Animation) -> Animation? {
            reduceMotion ? nil : animation
        }

        /// Cascade delay for a group of siblings appearing together — list rows, typing dots, stat tiles.
        /// One shared step keeps a cascade legible; per-site delays are how loops fall out of phase.
        /// Capped so a long list never makes the user wait on the tail.
        public static func stagger(_ index: Int, step: Double = 0.06, limit: Int = 8) -> Double {
            Double(min(max(0, index), limit)) * step
        }
    }

    // MARK: - Typography

    /// The type ramp. Default (SF Pro) rather than rounded — sharper, more precise, more premium.
    ///
    /// This is where a monochrome app lives or dies. With no hue to rank things by, *size and weight are the
    /// only hierarchy there is* — and the previous ramp had five roles sharing four sizes, two of them
    /// identical at 17pt, which is why every screen read as one undifferentiated block of body text.
    ///
    /// Nine sizes, each with its own weight, ordered so that adjacent roles are always distinguishable at a
    /// glance:
    ///
    /// | Role         | Style          | ≈pt | Weight    | Use for                                      |
    /// |--------------|----------------|-----|-----------|----------------------------------------------|
    /// | `display`    | .largeTitle    | 34  | bold      | one hero statement or number per screen      |
    /// | `title`      | .title2        | 22  | bold      | screen title, stat value, markdown h1        |
    /// | `titleSmall` | .title3        | 20  | semibold  | card and sheet titles                        |
    /// | `heading`    | .headline      | 17  | semibold  | **the section voice** — `SectionHeader`      |
    /// | `subheading` | .subheadline   | 15  | semibold  | sub-section voice, group labels              |
    /// | `body`       | .body          | 17  | regular   | prose, message text, row titles              |
    /// | `callout`    | .callout       | 16  | regular   | supporting prose inside a dense card         |
    /// | `footnote`   | .footnote      | 13  | regular   | row subtitles, helper text under a field     |
    /// | `caption`    | .caption       | 12  | regular   | labels, pills, dense annotations             |
    /// | `metadata`   | .caption2      | 11  | medium    | timestamps, counts, units — with `tertiaryText` |
    ///
    /// Every role is built from a system text style, never a fixed point size, so all of it scales with
    /// Dynamic Type. Pair with the ink tiers rather than restating hierarchy twice: a `footnote` in
    /// `secondaryText` is a subtitle; the same `footnote` in `tertiaryText` is metadata.
    public enum Typography {
        /// One per screen, at most. The greeting, the single number a screen exists to show. `display` used
        /// twice on one screen means neither is a display.
        public static let display    = Font.system(.largeTitle, design: .default).weight(.bold)
        /// Screen titles, stat values, markdown h1. Bold, not semibold, so it clears `heading` by weight as
        /// well as by size — a title that is only *bigger* still reads as body text at a glance.
        public static let title      = Font.system(.title2, design: .default).weight(.bold)
        /// The title of a self-contained thing: a card, a sheet, a detail header.
        public static let titleSmall = Font.system(.title3, design: .default).weight(.semibold)
        /// **The section voice.** Everything `SectionHeader` says at primary emphasis. Same size as `body`
        /// but semibold — which is the point: a section title must never be mistakable for the row text
        /// beneath it.
        public static let heading    = Font.system(.headline, design: .default)
        /// The quieter section voice — a sub-group inside a section, a column label. Used by
        /// `SectionHeader(emphasis: .secondary)`.
        public static let subheading = Font.system(.subheadline, design: .default).weight(.semibold)
        /// Prose, message bodies, row titles. The baseline everything else is measured against.
        public static let body       = Font.system(.body)
        /// Supporting prose where `body` would crowd — inside a dense card, a two-line explanation under a
        /// control. One notch down, still comfortably readable.
        public static let callout    = Font.system(.callout)
        /// Row subtitles and helper text. The *default* for a second line under a title — `caption` is a
        /// notch too small for a sentence a user is expected to actually read.
        public static let footnote   = Font.system(.footnote)
        /// Labels, pills, dense annotations. Short phrases, not sentences.
        public static let caption    = Font.system(.caption)
        /// Timestamps, counts, units, ids. Medium weight because at 11pt regular loses its shape; pair with
        /// `Colors.tertiaryText` so it recedes by colour rather than by being unreadably small.
        public static let metadata   = Font.system(.caption2, design: .default).weight(.medium)

        /// Code, ids, hashes, pairing codes — anything where character shape matters.
        public static let mono       = Font.system(.callout, design: .monospaced)
        /// Inline code and short machine strings inside running text.
        public static let monoSmall  = Font.system(.caption, design: .monospaced)
        /// A number that updates in place (a live count, a percentage, a timer). Monospaced digits only, so
        /// the layout doesn't twitch every time a `1` becomes a `7`.
        public static let numeral    = Font.system(.title2, design: .default).weight(.bold).monospacedDigit()
        /// The same, at metadata scale — a ticking count beside a label.
        public static let numeralSmall = Font.system(.footnote, design: .default).weight(.medium).monospacedDigit()
    }
}

public extension View {
    /// The canonical list treatment: Theme canvas behind the scroll. iOS's own grouped background is a
    /// *different* gray from the Theme canvas, so a screen that doesn't opt out visibly changes color tab
    /// to tab.
    ///
    /// This paints the CANVAS ONLY, and deliberately so. `.listRowBackground` does **not** propagate from
    /// a `List` down into its rows — verified on the simulator: a `List` carrying
    /// `.listRowBackground(.red)` renders red nowhere and its rows stay system-white. (The modifier styles
    /// the List *as a row of some outer list*, which is not what any call site here means.) It used to be
    /// applied here, and it was dead at all eighteen call sites; nothing looked wrong because iOS's row
    /// color and `Colors.surface` differ by about 1.5% — so the line survived by being both useless and
    /// invisible. It is gone rather than left to mislead the next author into believing a row is `surface`.
    ///
    /// What a plain row therefore is: `secondarySystemGroupedBackground` — pure white in light, which is
    /// the *raised* step, not the surface step. Two consequences worth knowing:
    ///   - Do not fill a card inside a plain row with `surfaceRaised`; it is white-on-white. Either give
    ///     the row `.listRowBackground(Color.clear)` and let the card sit on the canvas (what Activity and
    ///     the Inbox do), or pin the row with `saliRow()` so the card has somewhere to lift from.
    ///   - Inside a presented sheet, iOS resolves that same row color to a grey *darker* than the canvas,
    ///     which inverts the ramp. Sheets with rows want `saliRow()`.
    func saliList() -> some View {
        self
            .scrollContentBackground(.hidden)
            .background(Theme.Colors.background)
    }

    /// Pin a row to the Theme surface step. Apply to a `Section`, a `ForEach`, or a single row — anywhere
    /// *inside* the List content, which is the only place `.listRowBackground` is honored.
    ///
    /// Reach for it when a row must be a known step of the ramp rather than whatever iOS decides for the
    /// current presentation context: a row that contains a raised card, or any row inside a sheet.
    func saliRow() -> some View {
        listRowBackground(Theme.Colors.surface)
    }

    /// A standard raised surface card — the top of the elevation ramp, edged rather than shadowed.
    ///
    /// The hairline is not decoration: `surfaceRaised` is pure white on an off-white canvas in light mode,
    /// so the border is what actually states where the card ends. `Colors.border` (not `separator`) because
    /// this edge sits on the canvas, not inside an already-bounded surface.
    func saliCard(padding: CGFloat = Theme.Spacing.l,
                  radius: CGFloat = Theme.Radius.m) -> some View {
        self
            .padding(padding)
            .background(Theme.Colors.surfaceRaised)
            .clipShape(RoundedRectangle(cornerRadius: radius, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .strokeBorder(Theme.Colors.border, lineWidth: Theme.Stroke.hairline)
            )
    }

    /// The one hairline edge, for surfaces that draw their own background. `strokeBorder` rather than
    /// `stroke` so the line sits *inside* the shape and doesn't get clipped to half-width by an enclosing
    /// `clipShape` — the reason hand-rolled borders look inconsistently thin.
    func saliHairline(radius: CGFloat = Theme.Radius.m,
                      color: Color = Theme.Colors.border,
                      width: CGFloat = Theme.Stroke.hairline) -> some View {
        overlay(
            RoundedRectangle(cornerRadius: radius, style: .continuous)
                .strokeBorder(color, lineWidth: width)
        )
    }

    /// Fill with a named step of the elevation ramp, clipped and edged in one move. Use when a component is
    /// reusable enough that its caller should decide how high it sits.
    func saliSurface(_ level: Theme.Elevation,
                     radius: CGFloat = Theme.Radius.m,
                     bordered: Bool = true) -> some View {
        self
            .background(level.color)
            .clipShape(RoundedRectangle(cornerRadius: radius, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .strokeBorder(bordered ? Theme.Colors.border : .clear,
                                  lineWidth: Theme.Stroke.hairline)
            )
    }
}

#if canImport(UIKit)
extension UIColor {
    convenience init(rgb: UInt32) {
        self.init(
            red:   CGFloat((rgb >> 16) & 0xFF) / 255,
            green: CGFloat((rgb >> 8) & 0xFF) / 255,
            blue:  CGFloat(rgb & 0xFF) / 255,
            alpha: 1
        )
    }
}
#endif

extension Color {
    init(rgb: UInt32) {
        self.init(
            .sRGB,
            red:   Double((rgb >> 16) & 0xFF) / 255,
            green: Double((rgb >> 8) & 0xFF) / 255,
            blue:  Double(rgb & 0xFF) / 255,
            opacity: 1
        )
    }
}
