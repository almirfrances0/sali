import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

/// Routes between onboarding (no device authority yet) and the main tabbed control center.
///
/// The tab bar is the app's information architecture, so each tab answers exactly one question and no two
/// tabs answer the same one:
///
///     Chat    — talk to Sali (and where Sali talks to you: questions, consent, and proactive messages
///               arrive here as normal messages, and a plain reply answers them — handle_message resolves
///               a pending question/consent from free text, so there is no separate "inbox" to visit)
///     Now     — what is Sali doing, and what did it do?
///     Work    — the tasks
///     Sali    — everything about Sali itself
///
/// The Inbox tab was removed: it duplicated what the conversation already carries. Anything Sali needs from
/// Almir reaches him as a chat message, and he answers it the way he answers anything else.
public struct RootView: View {
    @EnvironmentObject private var appState: AppState
    @EnvironmentObject private var auth: AuthService
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    public init() {}

    public var body: some View {
        Group {
            // Logged in ⇔ a password is stored AND a server URL was entered. Both come from onboarding;
            // neither depends on a token, so an expired session never drops back to the login screen.
            if auth.isLoggedIn && appState.hasConfiguration {
                MainTabView()
            } else {
                OnboardingView()
            }
        }
        // The single most important transition in the app — onboarding becoming the control center —
        // is gated through `honoring` like every other Theme.Motion consumer, so a person who has asked
        // for stillness gets the state change with no cross-fade.
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle), value: auth.isLoggedIn)
        .onAppear { if auth.isLoggedIn && appState.hasConfiguration { appState.start() } }
    }
}

struct MainTabView: View {
    @EnvironmentObject private var appState: AppState

    var body: some View {
        TabShell(appState: appState)
    }
}

private struct TabShell: View {
    @ObservedObject var appState: AppState

    init(appState: AppState) {
        self.appState = appState
        // The bar's appearance has to be installed BEFORE the `UITabBar` is built, or the first frame is
        // the stock translucent grey with a blue tint and a red badge. A view initializer is the last
        // moment that is still true. Idempotent — see `SaliTabBarStyle`.
        MainActor.assumeIsolated { SaliTabBarStyle.install() }
    }

    var body: some View {
        TabView(selection: $appState.selectedTab) {
            ChatView()
                .tabItem { item(.chat) }
                .tag(AppTab.chat)

            NowView()
                .tabItem { item(.now) }
                .tag(AppTab.now)

            TasksView()
                .tabItem { item(.work) }
                .tag(AppTab.work)

            SaliHubView()
                .tabItem { item(.sali) }
                .tag(AppTab.sali)
        }
        .tint(Theme.Colors.accent)
        // Belt to the UIImage's braces. SwiftUI applies `.symbolVariant(.fill)` to tab items
        // automatically; this asks it not to. On its own it was NOT enough — the bar still came back
        // filled on device, which is why `item(_:)` builds a UIImage that the substitution cannot
        // reach. Kept because it costs nothing and covers any symbol added here later.
        .environment(\.symbolVariants, .none)
    }

    /// ONE OUTLINE GLYPH PER TAB, in both states.
    ///
    /// Selection used to swap the glyph for its `.fill` variant, which is what made the selected tab
    /// read as bold — a filled shape is a block of ink next to three line drawings, and it changes
    /// the SUBJECT of the icon, not just its state. Selection is now carried by the two signals that
    /// do not touch the drawing at all: the ink jumps from tertiary to full, and the label gains a
    /// weight step (both in `SaliTabBarStyle`).
    ///
    /// The glyph is built as a `UIImage`, NOT as `Image(systemName:)`, and that is the whole point.
    /// SwiftUI substitutes the `.fill` variant into any `Image(systemName:)` inside a `.tabItem`, and
    /// `.environment(\.symbolVariants, .none)` did not stop it here — the icons still came back
    /// filled on device. A `UIImage` never enters that substitution, so the symbol that is named is
    /// the symbol that draws. The explicit configuration also pins the WEIGHT to `.regular`, which is
    /// the other half of "not bold": the stock bar renders selected items heavier.
    private func item(_ tab: AppTab) -> some View {
        Label {
            Text(tab.title)
        } icon: {
            Image(uiImage: Self.glyph(tab.symbol))
        }
    }

    /// A template-rendered outline glyph at a fixed regular weight. Cached because `.tabItem` rebuilds
    /// on every selection change and `UIImage(systemName:)` is not free.
    @MainActor private static func glyph(_ name: String) -> UIImage {
        if let cached = glyphCache[name] { return cached }
        let config = UIImage.SymbolConfiguration(pointSize: 22, weight: .regular, scale: .medium)
        let image = (UIImage(systemName: name, withConfiguration: config) ?? UIImage())
            .withRenderingMode(.alwaysTemplate)
        glyphCache[name] = image
        return image
    }

    @MainActor private static var glyphCache: [String: UIImage] = [:]
}

// MARK: - Tab bar styling

/// Everything that makes the bottom bar a designed object rather than the system default, in one place.
///
/// It is a UIKit appearance rather than a hand-rolled SwiftUI bar on purpose: the stock bar is the only one
/// that keeps the platform's VoiceOver traits and rotor, its 44pt targets, its Dynamic-Type accessibility
/// HUD at the largest sizes, and its keyboard behaviour for the chat composer. So the *behaviour* stays
/// Apple's and the *drawing* becomes ours: an opaque Theme surface (never the translucent system grey),
/// one hairline top rule, three ink tiers, a weight step on the selected label, and a badge that is ink and
/// paper instead of a red blob.
///
/// The colours are written as literal pairs rather than read from `Theme.Colors` because they have to cross
/// into UIKit as *dynamic* `UIColor`s: a colour that resolves once, at install time, is a colour that is
/// wrong in the other scheme forever. Each one names the token it mirrors.
@MainActor
enum SaliTabBarStyle {
    private static var installed = false

    static func install() {
        #if canImport(UIKit)
        guard !installed else { return }
        installed = true

        let ink      = dyn(0x0B0B0C, 0xF5F5F6)   // Theme.Colors.accent / primaryText
        let quiet    = dyn(0x82828C, 0x77777F)   // Theme.Colors.tertiaryText
        let onInk    = dyn(0xFFFFFF, 0x0A0A0B)   // Theme.Colors.onAccent
        let surface  = dyn(0xF9F9FB, 0x161619)   // Theme.Colors.surface
        let hairline = dyn(0xE3E3E8, 0x2C2C32)   // Theme.Colors.separator

        let appearance = UITabBarAppearance()
        // Opaque, so the bar is one flat Theme surface with a single hairline above it — the system's
        // translucent blur samples whatever scrolls under it and reads as a different grey per screen.
        appearance.configureWithOpaqueBackground()
        appearance.backgroundColor = surface
        appearance.shadowColor = hairline

        // Scaled fonts, not fixed ones: a hardcoded point size is how a bar stops honoring Dynamic Type.
        let metrics = UIFontMetrics(forTextStyle: .caption2)
        let restingFont  = metrics.scaledFont(for: .systemFont(ofSize: 10, weight: .medium))
        let selectedFont = metrics.scaledFont(for: .systemFont(ofSize: 10, weight: .semibold))
        let badgeFont    = metrics.scaledFont(
            for: .monospacedDigitSystemFont(ofSize: 11, weight: .semibold))

        for layout in [appearance.stackedLayoutAppearance,
                       appearance.inlineLayoutAppearance,
                       appearance.compactInlineLayoutAppearance] {
            layout.normal.iconColor = quiet
            layout.normal.titleTextAttributes = [.foregroundColor: quiet, .font: restingFont]
            layout.selected.iconColor = ink
            layout.selected.titleTextAttributes = [.foregroundColor: ink, .font: selectedFont]

            // The badge is ink on paper in every state, at every selection — urgency is not a function of
            // which tab you happen to be standing on, and it is never a hue.
            for state in [layout.normal, layout.selected, layout.focused, layout.disabled] {
                state.badgeBackgroundColor = ink
                state.badgeTextAttributes = [.foregroundColor: onInk, .font: badgeFont]
            }
        }

        UITabBar.appearance().standardAppearance = appearance
        UITabBar.appearance().scrollEdgeAppearance = appearance
        #endif
    }

    #if canImport(UIKit)
    /// A `UIColor` that resolves per scheme at draw time — the UIKit half of `Theme.Colors.dyn`.
    private static func dyn(_ light: UInt32, _ dark: UInt32) -> UIColor {
        UIColor { trait in
            trait.userInterfaceStyle == .dark ? UIColor(rgb: dark) : UIColor(rgb: light)
        }
    }
    #endif
}

/// The "Sali" hub — everything *about* Sali rather than about the work: what it is carrying, what it knows,
/// what it has learned, when it wakes, how it is running, and how it is configured.
struct SaliHubView: View {
    @EnvironmentObject private var appState: AppState

    var body: some View {
        NavigationStack {
            List {
                Section {
                    NavigationLink { AgendaView() } label: {
                        Label("Agenda", systemImage: "target")
                    }
                    NavigationLink { DigitalWorldView() } label: {
                        Label("Digital world", systemImage: "globe")
                    }
                    // IntentAgendaView is the ONLY screen in the app that reads /goals, /initiatives,
                    // /obligations and /commitments — and nothing presented it, so those four endpoints
                    // had no reader on the phone at all. The section header above has always promised
                    // "goals, initiatives, promises"; this is the screen that actually shows them.
                    NavigationLink { IntentAgendaView() } label: {
                        Label("Goals & promises", systemImage: "list.bullet.rectangle")
                    }
                } header: {
                    SectionHeader("What Sali is carrying",
                                  subtitle: "Goals, initiatives, promises, and the accounts it maintains.")
                }

                Section {
                    NavigationLink { MemoryView() } label: {
                        Label("Memory & experiences", systemImage: "brain")
                    }
                    NavigationLink { LearningView() } label: {
                        Label("Learning & capabilities", systemImage: "graduationcap")
                    }
                    NavigationLink { SchedulesView() } label: {
                        Label("Schedules", systemImage: "calendar.badge.clock")
                    }
                } header: {
                    // Peer of "What Sali is carrying", so it carries the same rank and the same kind of
                    // subtitle. It used to be `.secondary` with no subtitle, which is the treatment for a
                    // group nested INSIDE a section — three sibling sections rendered at two different
                    // ranks read as an accident, and told the eye a hierarchy that isn't there.
                    SectionHeader("What Sali knows",
                                  subtitle: "What it remembers, what it has learned from its own work, "
                                          + "and the routines it keeps.")
                }

                Section {
                    NavigationLink { SystemView() } label: {
                        Label("System health", systemImage: "cpu")
                    }
                    NavigationLink { SettingsView() } label: {
                        Label("Settings & security", systemImage: "gearshape")
                    }
                } header: {
                    SectionHeader("How Sali runs",
                                  subtitle: "The machine it lives on, and the devices allowed to reach it.")
                }
            }
            .listStyle(.insetGrouped)
            .saliList()
            .navigationTitle("Sali")
            .toolbar {
                PresenceToolbarItem()
            }
        }
    }
}

// MARK: - Presence

/// The one "is this thing alive" indicator in the app, replacing the eight separate connection dots that
/// all said the same thing about a socket and nothing about Sali.
///
/// It is a CHIP, not a dot. A 12pt dot beside 11pt grey text is a status light, and a status light is the
/// cheapest object a UI can contain: it has no edges, no weight, and nothing to look at. This has a
/// hairline capsule, real internal air, a tracked uppercase label, and a fixed footprint — so it reads as a
/// considered part of the product in a toolbar, and the eye can find it in the same place every time.
///
/// **Aliveness is carried by four monochrome variables, never by a hue.**
///
///  1. *Continuity of the ring* — dashed when we cannot see Sali, unbroken when we can.
///  2. *Occupancy of its centre* — empty at idle (the negative space is the statement), a breathing core
///     while a reply streams, a turning arc while a tool runs, a solid core when Sali is held on you.
///  3. *Weight* — hairline while calm, `Stroke.emphasis` the moment Sali is actually doing something.
///  4. *Value* — the one state that is allowed to raise its voice inverts the whole chip to solid ink with
///     a paper label. Inversion is the loudest thing a monochrome system owns, so it is spent exactly once.
///
/// Motion is the fifth variable and the most disciplined one: there is **never more than one moving part**,
/// it only moves when Sali is genuinely busy, and it runs on `Theme.Motion`'s single beat — so the app is
/// still when Sali is still. Under Reduce Motion every state resolves to a distinct *still* frame; nothing
/// depends on movement to be readable.
///
/// Tapping it goes where the state can be acted on — `Needs you` lands in Chat (where Sali's question is,
/// and where a reply answers it), everything else in Now.
public struct PresenceBadge: View {
    @EnvironmentObject private var appState: AppState
    private let showsLabel: Bool

    public init(showsLabel: Bool = true) {
        self.showsLabel = showsLabel
    }

    public var body: some View {
        Button {
            appState.open(appState.presence.destination)
        } label: {
            PresenceChip(presence: appState.presence, showsLabel: showsLabel)
                // Refuse compression. Hiding the toolbar's shared background (see `PresenceToolbarItem`)
                // also makes the bar propose a TIGHTER width to the item, and the chip — which sizes
                // itself to its longest state so the bar never twitches — was compressed into that
                // proposal instead of keeping its own width. Measured on iPhone 17 Pro / iOS 27 with the
                // real caption2 label: "NEEDS YOU" rendered as "NEEDS Y…". That is the one state that
                // means Sali is blocked waiting on Almir, so it is the last one allowed to be unreadable.
                .fixedSize()
                .frame(minWidth: 44, minHeight: 44, alignment: .trailing)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Sali: \(appState.presence.label)")
        .accessibilityHint(appState.presence.explanation)
    }
}

/// The presence chip AS TOOLBAR CONTENT — the only way any screen should put it in a navigation bar.
///
/// **Why this exists: the nested-capsule bug.** From iOS 26 a toolbar item is given its own Liquid Glass
/// background, automatically, sized around whatever it contains. The chip already draws a capsule (it has
/// to — see `PresenceChip`), so a bare `ToolbarItem { PresenceChip(...) }` rendered a capsule inside a
/// capsule: two concentric rounded outlines with a visible gap between them, and on the System screen an
/// outer glass so nearly circular that it pinched the label.
///
/// The fix is the real API for it, `ToolbarContent.sharedBackgroundVisibility(.hidden)` (iOS 26+, declared
/// on both `ToolbarContent` and `CustomizableToolbarContent`), which tells the toolbar this item brings its
/// own background. Verified on iPhone 17 Pro / iOS 27 in both appearances: one capsule, no glass ring.
///
/// The alternatives were all worse. `ToolbarSpacer` only breaks items into *separate* glass groups — every
/// group still gets a background, so it cannot remove one. Dropping our capsule and letting the glass be
/// the shape is what produced the *earlier* failure: the system sizes that background around the label it
/// is given, so a glyph-plus-word came out as a pinched near-circle that the content overhung. A bare
/// `Text` in the item would inherit the glass too. And a `safeAreaInset` would move the indicator off the
/// bar the eye already looks at. Below iOS 26 there is no glass and nothing to suppress, so the item is
/// used as-is.
///
/// Placement is `.topBarTrailing` on every screen, so the indicator is in the same corner everywhere.
public struct PresenceToolbarItem: ToolbarContent {
    public init() {}

    public var body: some ToolbarContent {
        if #available(iOS 26.0, *) {
            ToolbarItem(placement: .topBarTrailing) { PresenceBadge() }
                .sharedBackgroundVisibility(.hidden)
        } else {
            ToolbarItem(placement: .topBarTrailing) { PresenceBadge() }
        }
    }
}

/// The chip itself, without the button — so a screen that already owns the tap (the Now hero) can render
/// the same object without nesting one control inside another.
struct PresenceChip: View {
    let presence: SaliPresence
    var showsLabel: Bool = true

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .caption2) private var glyphSide: CGFloat = 13

    var body: some View {
        ZStack {
            if showsLabel { widthGhost }
            HStack(spacing: Theme.Spacing.s) {
                SaliStateGlyph(presence.glyph, animated: !reduceMotion, side: glyphSide, ink: ink)
                if showsLabel { text(presence.label) }
            }
        }
        .padding(.horizontal, showsLabel ? Theme.Spacing.s : Theme.Spacing.xs)
        .padding(.vertical, Theme.Spacing.xs)
        .background(Capsule(style: .continuous).fill(fill))
        .overlay(Capsule(style: .continuous).strokeBorder(stroke, lineWidth: strokeWidth))
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick), value: presence)
    }

    /// **One width, and the content CENTRED in it.** The chip reserves the room the longest state needs
    /// ("Needs you", ~70pt of label) so its silhouette is identical for the life of the app — a status
    /// indicator that resizes as it changes meaning makes the whole toolbar twitch, and reads as a layout
    /// bug rather than as information.
    ///
    /// The ghost is an invisible copy of the *whole* content — glyph column included — rather than just the
    /// label, and the real content is centred over it. That is the difference between a pill and a pill with
    /// a hole in it: leading-aligning the content inside a box sized for "NEEDS YOU" left 50pt of dead air
    /// to the right of "IDLE" against 8pt on the left, a 6:1 asymmetry that read as a truncated or
    /// half-drawn element in the one state the app is in almost all the time. Centring splits that air
    /// evenly (~29pt each side at idle) and costs only a ≤21pt lateral shift of the glyph on a real state
    /// change, which is animated and rarer than the still frame everyone actually looks at.
    ///
    /// Only the ghost's *width* is used — it is `.hidden()`, so it draws nothing and is invisible to
    /// VoiceOver — and it holds no `SaliStateGlyph`, only a spacer the glyph's exact size, so there is still
    /// exactly one live glyph (and one ambient loop) per chip.
    private var widthGhost: some View {
        HStack(spacing: Theme.Spacing.s) {
            Color.clear.frame(width: glyphSide, height: glyphSide)
            ZStack {
                ForEach(SaliPresence.allCases, id: \.self) { state in
                    text(state.label)
                }
            }
        }
        .hidden()
    }

    private func text(_ value: String) -> some View {
        Text(value.uppercased())
            .font(Theme.Typography.metadata)
            .tracking(0.6)
            .lineLimit(1)
            .foregroundStyle(ink)
    }

    /// The single ink the chip is drawn in — glyph, label and all. Escalates by tier, not by hue: quiet at
    /// the edge of legibility when Sali is offline, full ink the moment it is doing something, and paper
    /// when the chip itself has gone to ink.
    private var ink: Color {
        switch presence {
        case .offline:            Theme.Colors.tertiaryText
        case .idle, .blocked:     Theme.Colors.secondaryText
        case .thinking, .working: Theme.Colors.primaryText
        case .waitingOnYou:       Theme.Colors.onAccent
        }
    }

    private var fill: Color {
        presence.needsYou ? Theme.Colors.accent : .clear
    }

    private var stroke: Color {
        switch presence {
        case .waitingOnYou:       .clear                      // the fill is the edge
        case .offline:            Theme.Colors.separator
        case .idle:               Theme.Colors.border
        case .thinking, .working, .blocked: Theme.Colors.borderStrong
        }
    }

    /// The edge thickens while Sali is under its own power. It is a static signal, so it survives Reduce
    /// Motion and a still screenshot alike.
    private var strokeWidth: CGFloat {
        presence.isBusy ? Theme.Stroke.emphasis : Theme.Stroke.hairline
    }
}

#Preview {
    VStack(alignment: .trailing, spacing: Theme.Spacing.m) {
        ForEach(SaliPresence.allCases, id: \.self) { state in
            PresenceChip(presence: state)
        }
    }
    .padding(Theme.Spacing.xl)
    .frame(maxWidth: .infinity, maxHeight: .infinity)
    .background(Theme.Colors.background)
}
