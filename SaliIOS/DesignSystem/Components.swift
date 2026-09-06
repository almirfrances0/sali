import SwiftUI
import Foundation
#if canImport(UIKit)
import UIKit
#endif

// Reusable, state-complete building blocks (§37). Every list/screen uses these so loading / empty / error /
// offline all look intentional — a polished app stays coherent even when there's no data.

/// The one primary action style: an ink fill with a paper label. Because `accent` is adaptive ink
/// (near-black in light, near-white in dark), the label MUST be `onAccent` — a hardcoded white would vanish
/// in dark mode. Callers that want full width put `.frame(maxWidth: .infinity)` on their label.
struct SaliPrimaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View { Content(configuration: configuration) }

    struct Content: View {
        let configuration: ButtonStyleConfiguration
        @Environment(\.isEnabled) private var isEnabled
        @Environment(\.accessibilityReduceMotion) private var reduceMotion
        var body: some View {
            configuration.label
                .font(Theme.Typography.body.weight(.semibold))
                .foregroundStyle(Theme.Colors.onAccent)
                .padding(.vertical, Theme.Spacing.m)
                .padding(.horizontal, Theme.Spacing.l)
                .background(Theme.Colors.accent)
                .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.m, style: .continuous))
                .opacity(isEnabled ? (configuration.isPressed ? 0.86 : 1) : 0.35)
                .scaleEffect(configuration.isPressed ? 0.99 : 1)
                // The canonical discrete-feedback pattern: a Theme token, gated through `honoring`, so
                // the press still registers instantly for anyone who has asked for stillness.
                .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick), value: configuration.isPressed)
                .contentShape(Rectangle())
        }
    }
}

/// A small pill of state. Monochrome by DEFAULT (item 12): a healthy app must not read as a wall of
/// green and amber, so `ok`/`warn`/`danger` are reserved for states that genuinely need the user —
/// failed, blocked, waiting-on-you, critical. Everything else is ink or secondary ink.
public struct StatusPill: View {
    let text: String
    let color: Color
    public init(_ text: String, color: Color = Theme.Colors.secondaryText) {
        self.text = text; self.color = color
    }
    public var body: some View {
        Text(text)
            .font(Theme.Typography.caption.weight(.medium))
            .padding(.horizontal, Theme.Spacing.s)
            .padding(.vertical, Theme.Spacing.xs)
            .background(color.opacity(0.16))
            .foregroundStyle(color)
            .clipShape(Capsule())
    }
}

// MARK: - The one state vocabulary

/// Every state this app draws — presence, the socket, a chat turn — is ONE of these eight shapes.
///
/// There used to be three parallel drawings of the same idea: `ConnectionBadge`'s 7pt dot, `PresenceGlyph`
/// in the toolbar, and `ChatStateGlyph` in the chat header. They disagreed about what "busy" looks like
/// (a 0.34-wide breathing core here, a 0.64-wide solid disc there) and, worse, about what to *call* it
/// ("Live" vs "IDLE", "Thinking" vs "Replying"). Two indicators in one app are not two features; they are
/// one feature and one bug. This enum is the single alphabet, and `SaliStateGlyph` is the single hand
/// that writes it.
///
/// The alphabet is deliberately small and strictly monochrome. Every letter is the SAME circle at the SAME
/// diameter; only three things vary, none of them hue:
///
///  1. *Continuity of the ring* — dashed when we cannot see Sali, open while we are on the way, unbroken
///     once we are there.
///  2. *Occupancy of its centre* — empty when nothing is happening (the negative space is the statement),
///     progressively more of the circle filled as more of Sali is engaged.
///  3. *Weight* — hairline while calm, `Stroke.emphasis` the moment Sali is doing something or asking
///     something of you.
public enum SaliGlyph: String, Equatable, Hashable, Sendable, CaseIterable {
    /// We cannot see Sali at all. Whatever is drawn elsewhere is the last thing we were told, not news.
    case offline
    /// On the way — a socket-facing state that presence itself does not have.
    case connecting
    /// Present, doing nothing. An unbroken ring around nothing at all.
    case idle
    /// Held behind Sali's current work — a chat-facing state that presence itself does not have.
    case queued
    /// A reply is streaming. Named for what it is derived from (`message.started`), never "thinking".
    case replying
    /// A tool or task is running under Sali's own power.
    case working
    /// Blocked on *you*. The one state allowed to raise its voice.
    case needsYou
    /// Sali stopped itself rather than guess.
    case paused

    /// The default ink ramp for a glyph drawn on the canvas (not inside a filled chip). Escalation by
    /// TIER, never by hue: at the edge of legibility when we cannot see Sali, full ink when it is working
    /// or waiting on you.
    public var ink: Color {
        switch self {
        case .offline, .connecting:  Theme.Colors.tertiaryText
        case .idle, .queued, .paused: Theme.Colors.secondaryText
        case .replying, .working, .needsYou: Theme.Colors.primaryText
        }
    }

    /// Whether this letter has an ambient loop at all. Everything else is a still frame by construction,
    /// so the app is still whenever Sali is still.
    public var isAmbient: Bool { self == .connecting || self == .replying || self == .working }
}

/// Sali's state as a shape, in one ink. The ONE drawing of `SaliGlyph`, used by the presence chip, the
/// Now masthead, the chat header, and the connection row in Settings — so a state that means the same
/// thing looks the same everywhere, at every size.
///
/// Motion is the most disciplined variable here: there is **never more than one moving part**, it only
/// moves while Sali is genuinely busy, and it runs on `Theme.Motion`'s single beat, so nothing can drift
/// against the mark in the transcript. Under Reduce Motion (or `animated: false`) every state resolves to
/// a distinct *still* frame — nothing depends on movement to be readable.
public struct SaliStateGlyph: View {
    let glyph: SaliGlyph
    /// Whether the ambient loop may run. Callers pass `!reduceMotion`, and the chat header additionally
    /// withholds it while a live turn's own mark is already the one moving thing on screen.
    var animated: Bool = true
    /// The square the glyph occupies. Identical in every state, so the indicator never resizes as it
    /// changes meaning — a status light that moves the layout reads as a rendering fault.
    var side: CGFloat = 13
    /// The single ink to draw in, passed down so the glyph can invert inside a filled chip.
    var ink: Color = Theme.Colors.primaryText

    public init(_ glyph: SaliGlyph,
                animated: Bool = true,
                side: CGFloat = 13,
                ink: Color? = nil) {
        self.glyph = glyph
        self.animated = animated
        self.side = side
        self.ink = ink ?? glyph.ink
    }

    @State private var turn = false
    @State private var breathe = false

    public var body: some View {
        ZStack {
            ring
            core
        }
        .frame(width: side, height: side)
        .onAppear { startAmbient() }
        .onChange(of: glyph) { _, _ in startAmbient() }
        .onChange(of: animated) { _, _ in startAmbient() }
        // Always mute: every call site says this in words right next to it, and a VoiceOver user should
        // hear the state once, not twice.
        .accessibilityHidden(true)
    }

    /// The outline. Continuity says whether we can see Sali at all; weight says how much it is asking of you.
    @ViewBuilder private var ring: some View {
        switch glyph {
        case .offline:
            // Broken line, same circle: the ring is still exactly where it always is — the *continuity*
            // is what is missing, which is precisely what "we cannot see Sali" means. The dash is
            // proportional so it survives Dynamic Type instead of turning into a dotted smear.
            Circle().strokeBorder(ink, style: StrokeStyle(lineWidth: Theme.Stroke.hairline,
                                                          dash: [side * 0.11, side * 0.17]))
        case .connecting:
            // An open ring, turning: on its way, not arrived.
            Circle()
                .trim(from: 0, to: 0.7)
                .stroke(ink, style: StrokeStyle(lineWidth: Theme.Stroke.hairline, lineCap: .round))
                .padding(Theme.Stroke.hairline / 2)
                .rotationEffect(.degrees(turn ? 360 : 0))
        case .working:
            ZStack {
                // The full ring stays, faintly, so the shape never loses mass while the arc travels —
                // an arc alone spinning in empty space is a spinner, and a spinner is not a presence.
                Circle().strokeBorder(ink.opacity(0.22), lineWidth: Theme.Stroke.hairline)
                Circle()
                    .trim(from: 0, to: 0.30)
                    .stroke(ink, style: StrokeStyle(lineWidth: Theme.Stroke.emphasis, lineCap: .round))
                    .padding(Theme.Stroke.emphasis / 2)
                    .rotationEffect(.degrees(turn ? 360 : 0))
            }
        case .needsYou:
            Circle().strokeBorder(ink, lineWidth: Theme.Stroke.emphasis)
        case .idle, .queued, .replying, .paused:
            Circle().strokeBorder(ink, lineWidth: Theme.Stroke.hairline)
        }
    }

    /// What occupies the ring — the actual sentence the glyph speaks, written in negative space.
    @ViewBuilder private var core: some View {
        switch glyph {
        case .offline, .idle, .connecting:
            // Deliberately empty. An idle Sali that still shows a filled dot is an app insisting
            // something is happening; the hollow ring is the honest drawing of "present, doing nothing".
            EmptyView()
        case .queued:
            Circle().fill(ink).frame(width: side * 0.28, height: side * 0.28)
        case .replying:
            Circle()
                .fill(ink)
                .frame(width: side * 0.34, height: side * 0.34)
                // Rest is FULL, and the breath moves away from it and back, so Reduce Motion's still
                // frame is the complete core rather than a permanently half-faded one.
                .scaleEffect(breathe ? 1.24 : 1.0)
                .opacity(breathe ? 0.5 : 1.0)
        case .working:
            Circle().fill(ink).frame(width: side * 0.24, height: side * 0.24)
        case .needsYou:
            Circle().fill(ink).frame(width: side * 0.42, height: side * 0.42)
        case .paused:
            // A bar across the middle: the ring is intact, the way through it is not.
            Capsule().fill(ink).frame(width: side * 0.46, height: Theme.Stroke.emphasis)
        }
    }

    /// One moving part, only while Sali is genuinely busy, always on `Theme.Motion`'s single beat. The
    /// guard bails out BEFORE touching the driving state, because a `repeatForever` gated to `nil` would
    /// still set the flag and the glyph would snap to the end pose and hold it.
    private func startAmbient() {
        guard animated else {
            turn = false
            breathe = false
            return
        }
        switch glyph {
        case .working, .connecting:
            breathe = false
            turn = false
            // `sweep` (1 beat), not `orbit` (4 beats): at 13pt an orbit revolution is too slow to read
            // as motion at all. Both are whole multiples of Motion.beat, so they stay in phase.
            withAnimation(Theme.Motion.sweep) { turn = true }
        case .replying:
            turn = false
            breathe = false
            withAnimation(Theme.Motion.breathing) { breathe = true }
        case .offline, .idle, .queued, .needsYou, .paused:
            turn = false
            breathe = false
        }
    }
}

/// The socket, as a fact — and the ONLY place in the app that still talks about a socket.
///
/// **Why this survives at all.** Presence (`SaliPresence`) is the subject everywhere else, and it already
/// collapses to `.offline` the moment the socket drops, so a second "are we connected" indicator beside
/// it would say nothing new in a different accent. The one exception is Settings' Connection section,
/// where the transport itself IS the subject: the row reads "Connected to <host>", the person is choosing
/// *which* server to talk to, and "IDLE" would be an answer to a question nobody asked. So the badge is
/// scoped to that row and drawn from the same alphabet as everything else — same ring, same tracked
/// uppercase, same ink ramp — so it reads as the same family, not a different species.
///
/// It owns NO capsule: it lives inside a list row that is already a surface. The chip's capsule is for
/// the toolbar, where the thing has to hold its own edge.
/// What the badge needs to know about a connection, and nothing else. Two layers answer "are we
/// connected" — `ConnectionState` (is the socket open) and `LinkState` (which runtime, over which
/// transport) — and both are legitimate subjects for this badge depending on the screen. Rather than
/// duplicate the drawing per layer, or force a lossy conversion between two enums that mean different
/// things, each conforms and the badge stays the single hand that writes them.
public protocol ConnectionBadgeState {
    /// The state in the app's one alphabet.
    var glyph: SaliGlyph { get }
    /// Short enough for tracked uppercase.
    var shortLabel: String { get }
    /// The long, spoken form — what VoiceOver reads.
    var label: String { get }
    /// Full ink, or quiet.
    var isLive: Bool { get }
}

public struct ConnectionBadge: View {
    private let glyph: SaliGlyph
    private let shortLabel: String
    private let spokenLabel: String
    private let isLive: Bool
    var showsLabel: Bool

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .caption2) private var glyphSide: CGFloat = 13

    public init(_ state: some ConnectionBadgeState, showsLabel: Bool = true) {
        self.glyph = state.glyph
        self.shortLabel = state.shortLabel
        self.spokenLabel = state.label
        self.isLive = state.isLive
        self.showsLabel = showsLabel
    }

    public var body: some View {
        HStack(spacing: Theme.Spacing.s) {
            SaliStateGlyph(glyph, animated: !reduceMotion, side: glyphSide, ink: ink)
            if showsLabel {
                Text(shortLabel.uppercased())
                    .font(Theme.Typography.metadata)
                    .tracking(0.6)
                    .lineLimit(1)
                    .foregroundStyle(ink)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Connection: \(spokenLabel)")
    }

    /// The same tier ramp the presence chip uses: quiet while we have nothing, full ink once we do.
    private var ink: Color {
        isLive ? Theme.Colors.primaryText : Theme.Colors.tertiaryText
    }
}

/// The ONE section header in the app (item 6). Two styles used to be mixed — sometimes inside the same
/// scroll view — because a bare `Section("…")` gets SwiftUI's uppercased caption while this one doesn't.
/// So this carries its own `.textCase(nil)`, insets aligned to inset-grouped content
/// (`Theme.Spacing.listMargin`), and a clear row background.
///
/// **Two voices, because 49 call sites are not all the same rank.** A screen with eight equally loud
/// headers has no hierarchy at all; `.secondary` is for a group *inside* a section — a sub-list, a set of
/// options under a heading that already said what this area is.
///
/// **The rhythm is self-contained.** It used to live entirely in `listRowInsets`, which is a no-op outside
/// a `List` — so the identical header dropped into a `ScrollView` lost every bit of its spacing and
/// collided with whatever came before it. Now the vertical rhythm is real padding on the content and only
/// the *horizontal* inset comes from `listRowInsets`, so the header occupies the same space in a `List`,
/// a `ScrollView`, a `VStack`, or a card.
///
/// Because the header owns its own top and bottom air, a stack around it should not add more: use
/// `VStack(spacing: 0)` and let the headers set the rhythm, or the gaps compound.
public struct SectionHeader: View {
    /// Rank, not decoration. `primary` names an area of the screen; `secondary` names a group inside one.
    public enum Emphasis: Sendable {
        /// The section voice: `Typography.heading`, full ink. One per area of the screen.
        case primary
        /// The sub-section voice: smaller, still semibold so it can't be mistaken for row text, in
        /// secondary ink, and set tighter to the content it introduces.
        case secondary
    }

    let title: String
    let subtitle: String?
    let emphasis: Emphasis

    public init(_ title: String, subtitle: String? = nil, emphasis: Emphasis = .primary) {
        self.title = title
        self.subtitle = subtitle
        self.emphasis = emphasis
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(titleFont)
                .foregroundStyle(titleInk)
            if let subtitle {
                // `footnote`, not `caption`: a header subtitle is a sentence the user is meant to read
                // ("Sali resumes this task as soon as you answer"), and 12pt is a notch too small for that.
                // Ink follows rank — a primary section's subtitle still supports, a sub-section's recedes.
                Text(subtitle)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(emphasis == .primary
                                     ? Theme.Colors.secondaryText
                                     : Theme.Colors.tertiaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, topSpace)
        .padding(.bottom, bottomSpace)
        .textCase(nil)
        // Horizontal only. Vertical rhythm is padding above, so it survives outside a `List`.
        .listRowInsets(EdgeInsets(top: 0, leading: Theme.Spacing.listMargin,
                                  bottom: 0, trailing: Theme.Spacing.listMargin))
        .listRowBackground(Color.clear)
        .listRowSeparator(.hidden)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }

    private var titleFont: Font {
        switch emphasis {
        case .primary:   Theme.Typography.heading
        case .secondary: Theme.Typography.subheading
        }
    }
    private var titleInk: Color {
        switch emphasis {
        case .primary:   Theme.Colors.primaryText
        case .secondary: Theme.Colors.secondaryText
        }
    }
    /// A new section needs a real break above it; a sub-group only needs to be told apart from the block
    /// it follows.
    private var topSpace: CGFloat {
        switch emphasis {
        case .primary:   Theme.Spacing.l
        case .secondary: Theme.Spacing.m
        }
    }
    /// Always tighter than the space above — a header must sit closer to what it labels than to what it
    /// follows, or the eye attaches it to the wrong content.
    private var bottomSpace: CGFloat {
        switch emphasis {
        case .primary:   Theme.Spacing.s
        case .secondary: Theme.Spacing.xs
        }
    }
}

/// The counterpart `SectionHeader` never had. A section that needs a caveat, a total, or an explanation of
/// what a control will actually do had nowhere to put it, so that text ended up as an extra row — which
/// reads as content the user has to act on rather than as a note about the section.
///
/// Mirrors the header exactly: same horizontal inset, same self-contained vertical rhythm, quieter voice.
/// Its top padding is small (it belongs to the section above it) and its bottom padding is the real break
/// before whatever comes next.
public struct SectionFooter: View {
    let text: String
    /// Optional trailing metadata — a count, a total, a last-updated stamp — kept on the same line so the
    /// footer stays one visual object.
    let detail: String?

    public init(_ text: String, detail: String? = nil) {
        self.text = text
        self.detail = detail
    }

    public var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
            Text(text)
                .font(Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.secondaryText)
                .fixedSize(horizontal: false, vertical: true)
            if let detail {
                Spacer(minLength: Theme.Spacing.s)
                Text(detail)
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .monospacedDigit()
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, Theme.Spacing.s)
        .padding(.bottom, Theme.Spacing.l)
        .textCase(nil)
        .listRowInsets(EdgeInsets(top: 0, leading: Theme.Spacing.listMargin,
                                  bottom: 0, trailing: Theme.Spacing.listMargin))
        .listRowBackground(Color.clear)
        .listRowSeparator(.hidden)
        .accessibilityElement(children: .combine)
    }
}

/// The canonical *indeterminate* wait — for moments with no shape to promise yet (an inline recall, a
/// short search). Anything that will resolve into a known layout should use `SaliSkeleton…` instead, so
/// the screen never hard-swaps from a bare spinner to full content. Wears the Sali mark rather than the
/// system spinner; the mark honors `accessibilityReduceMotion` itself.
public struct LoadingState: View {
    let message: String
    @ScaledMetric(relativeTo: .title2) private var markSize: CGFloat = 32
    public init(_ message: String = "Loading…") { self.message = message }
    public var body: some View {
        VStack(spacing: Theme.Spacing.m) {
            SaliMark(size: markSize, color: Theme.Colors.secondaryText, animated: true)
            Text(message)
                .font(Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.secondaryText)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(message)
    }
}

/// The canonical empty state — coherent even when Sali has nothing to show. The DEFAULT glyph is the
/// Sali mark (item 10): the identity, not a borrowed `sparkles`. A concrete symbol is still worth passing
/// when it says something the mark can't ("bell.slash", "lock.shield").
public struct EmptyStateView: View {
    let icon: String?
    let title: String
    let message: String
    @ScaledMetric(relativeTo: .largeTitle) private var glyphSize: CGFloat = 40

    public init(icon: String? = nil, title: String, message: String) {
        self.icon = icon; self.title = title; self.message = message
    }
    public var body: some View {
        VStack(spacing: Theme.Spacing.s) {
            glyph
                .padding(.bottom, Theme.Spacing.xs)
            // `titleSmall`, not `heading`: this is the one statement on an otherwise empty screen, and at
            // `heading` it sat at exactly the weight of the sentence beneath it — nothing led.
            Text(title)
                .font(Theme.Typography.titleSmall)
                .foregroundStyle(Theme.Colors.primaryText)
            Text(message)
                .font(Theme.Typography.callout)
                .foregroundStyle(Theme.Colors.secondaryText)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, Theme.Spacing.xl)
        .padding(.vertical, Theme.Spacing.heroGap)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    @ViewBuilder private var glyph: some View {
        if let icon {
            Image(systemName: icon).font(.system(size: glyphSize))
                .foregroundStyle(Theme.Colors.secondaryText)
                .accessibilityHidden(true)
        } else {
            SaliMark(size: glyphSize, color: Theme.Colors.secondaryText)
        }
    }
}

/// The canonical error / offline state with a retry.
public struct ErrorStateView: View {
    let message: String
    let retry: (() -> Void)?
    @ScaledMetric(relativeTo: .largeTitle) private var glyphSize: CGFloat = 34
    public init(_ message: String, retry: (() -> Void)? = nil) { self.message = message; self.retry = retry }
    public var body: some View {
        VStack(spacing: Theme.Spacing.m) {
            Image(systemName: "wifi.exclamationmark").font(.system(size: glyphSize, weight: .light))
                .foregroundStyle(Theme.Colors.tertiaryText)
            Text(message)
                .font(Theme.Typography.callout)
                .foregroundStyle(Theme.Colors.secondaryText)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            if let retry {
                Button("Try again", action: retry).buttonStyle(SaliPrimaryButtonStyle())
                    .padding(.top, Theme.Spacing.s)
            }
        }
        .padding(.horizontal, Theme.Spacing.xl)
        .padding(.vertical, Theme.Spacing.heroGap)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// A generic async-loadable container that renders the right state for a `Loadable`.
public enum Loadable<T> {
    case idle, loading, loaded(T), failed(String)
}

public struct LoadableView<T, Content: View>: View {
    let state: Loadable<T>
    let retry: (() -> Void)?
    let content: (T) -> Content
    public init(_ state: Loadable<T>, retry: (() -> Void)? = nil, @ViewBuilder content: @escaping (T) -> Content) {
        self.state = state; self.retry = retry; self.content = content
    }
    public var body: some View {
        switch state {
        case .idle, .loading: SaliSkeletonList()
        case .loaded(let value): content(value)
        case .failed(let message): ErrorStateView(message, retry: retry)
        }
    }
}

/// A compact 0...1 confidence indicator (§16/§40 — every memory carries provenance + confidence +
/// freshness, and it is always surfaced honestly, never as false precision). Used by Memory & Learning.
public struct ConfidenceBar: View {
    let value: Double
    public init(_ value: Double) { self.value = max(0, min(1, value)) }
    public var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                // The empty part of the bar is placeholder geometry, not a divider — `placeholder` is the
                // one token guaranteed to stay visible on every step of the elevation ramp.
                Capsule().fill(Theme.Colors.placeholder)
                Capsule().fill(tint).frame(width: max(3, proxy.size.width * value))
            }
        }
        .frame(height: 6)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Confidence")
        .accessibilityValue("\(Int((value * 100).rounded())) percent")
    }
    /// Ink at three weights, never three hues (item 12): confidence is already carried by LENGTH, so the
    /// color only has to say "faint / medium / firm" — and it stays legible in light and dark alike.
    private var tint: Color {
        switch value {
        case ..<0.34: Theme.Colors.accent.opacity(0.35)
        case ..<0.67: Theme.Colors.accent.opacity(0.62)
        default: Theme.Colors.accent
        }
    }
}

/// A deliberate, natural-language confirmation for a destructive or consequential action (§37) — never a
/// one-tap toggle. Explains what will happen in plain language before the user commits, and requires an
/// explicit second tap to proceed.
public struct NaturalConfirmationSheet: View {
    let title: String
    let explanation: String
    let confirmLabel: String
    let isDestructive: Bool
    let confirmDisabled: Bool
    let onConfirm: () -> Void
    @Environment(\.dismiss) private var dismiss
    @ScaledMetric(relativeTo: .title) private var glyphSize: CGFloat = 32

    public init(title: String, explanation: String, confirmLabel: String, isDestructive: Bool = true,
                confirmDisabled: Bool = false, onConfirm: @escaping () -> Void) {
        self.title = title
        self.explanation = explanation
        self.confirmLabel = confirmLabel
        self.isDestructive = isDestructive
        self.confirmDisabled = confirmDisabled
        self.onConfirm = onConfirm
    }

    public var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: Theme.Spacing.l) {
                Image(systemName: isDestructive ? "exclamationmark.triangle.fill" : "checkmark.seal")
                    .font(.system(size: glyphSize))
                    .foregroundStyle(isDestructive ? Theme.Colors.danger : Theme.Colors.accent)
                Text(explanation)
                    .font(Theme.Typography.body)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                if isDestructive {
                    Button(role: .destructive) {
                        onConfirm(); dismiss()
                    } label: {
                        Text(confirmLabel).frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(Theme.Colors.danger)
                    .disabled(confirmDisabled)
                } else {
                    Button { onConfirm(); dismiss() } label: {
                        Text(confirmLabel).frame(maxWidth: .infinity)
                    }
                    .buttonStyle(SaliPrimaryButtonStyle())
                    .disabled(confirmDisabled)
                }
                Button("Cancel", role: .cancel) { dismiss() }
                    .frame(maxWidth: .infinity)
                    .tint(Theme.Colors.secondaryText)
            }
            .padding(Theme.Spacing.xl)
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
        }
        .presentationDetents([.medium])
    }
}

/// The one haptic vocabulary (§37). Tactile feedback is a design-system decision, not a per-screen one:
/// a consequential commit should feel the same wherever it happens, so the whole app speaks one language
/// instead of firing an ad-hoc generator on whichever screen an author happened to remember.
///
///  - `light`   — a confirmation: a message sent, a quick reply chosen, a copy, a disclosure opening.
///                The default "it registered".
///  - `firm`    — a weighty state change the person should FEEL land: pausing / resuming / cancelling
///                Sali's work, stopping a stream, switching the model, granting consent.
///  - `success` — a meaningful completion worth marking: a device paired, an ask answered and sent.
///  - `failure` — something did not go through: a send that couldn't reach Sali, an action that errored.
///
/// A no-op where UIKit is unavailable (previews on other platforms), so call sites stay unconditional.
public enum Haptics {
    #if canImport(UIKit)
    @MainActor public static func light()   { UIImpactFeedbackGenerator(style: .light).impactOccurred() }
    @MainActor public static func firm()    { UIImpactFeedbackGenerator(style: .rigid).impactOccurred() }
    @MainActor public static func success() { UINotificationFeedbackGenerator().notificationOccurred(.success) }
    @MainActor public static func failure() { UINotificationFeedbackGenerator().notificationOccurred(.error) }
    #else
    public static func light() {}
    public static func firm() {}
    public static func success() {}
    public static func failure() {}
    #endif
}

public extension Date {
    /// True when this date is the decoder's "I could not read that" sentinel rather than a real instant.
    ///
    /// `APIClient.decoder` returns `.distantPast` for a timestamp it cannot parse, because throwing would
    /// fail the whole response and blank a screen over one bad field. That trade is right, but it means a
    /// sentinel can reach the UI, and a sentinel rendered as a date is a lie — `RelativeDateTimeFormatter`
    /// turns it into a confident "56 yr. ago". Anything that formats a decoded date checks this first.
    var saliIsUnknownTime: Bool { self == .distantPast }

    /// A short, natural relative description ("2 hours ago") — used wherever the design calls for a
    /// freshness cue rather than a raw timestamp.
    ///
    /// Deliberately a trailing fragment ("at an unknown time"), because almost every call site embeds it
    /// after a verb: "Updated …", "checked …", "Sali checks again …". Say the value is unknown; never
    /// invent one.
    var saliRelative: String {
        if saliIsUnknownTime { return "at an unknown time" }
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .short
        return f.localizedString(for: self, relativeTo: Date())
    }
}

// MARK: - Soft loading (item 1)

/// One placeholder bar: a rounded rectangle in `placeholder` ink with a single, very quiet monochrome sweep
/// travelling across it. This is the atom every skeleton is built from.
///
/// It fills with `Colors.placeholder`, not `surfaceRaised`. Now that elevation always moves toward the
/// light, `surfaceRaised` is pure white in light mode — a skeleton bar drawn in it *inside a card* would be
/// white on white and simply vanish. `placeholder` is defined to stay visible on every step of the ramp.
///
/// Two rules it never breaks. It is strictly monochrome — the sweep is `primaryText` at 6%, so it reads as
/// "slightly lighter ink" in light mode and "slightly lighter paper" in dark, and never introduces a hue.
/// And it holds still for anyone who has asked it to: with `accessibilityReduceMotion` on, the bar renders
/// as a plain resting shape, the sweep is not in the view tree at all, and the driving state is never set —
/// the full ambient pattern from `Theme.Motion`.
public struct SaliSkeleton: View {
    private let width: CGFloat?
    private let height: CGFloat
    private let radius: CGFloat

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var phase: CGFloat = -1

    public init(width: CGFloat? = nil, height: CGFloat = 12, radius: CGFloat = Theme.Radius.xs) {
        self.width = width
        self.height = height
        self.radius = radius
    }

    public var body: some View {
        let shape = RoundedRectangle(cornerRadius: radius, style: .continuous)
        shape
            .fill(Theme.Colors.placeholder)
            .frame(width: width, height: height)
            .frame(maxWidth: width == nil ? .infinity : nil, alignment: .leading)
            .overlay { if !reduceMotion { sweep } }
            .clipShape(shape)
            .accessibilityHidden(true)
            .onAppear {
                guard !reduceMotion else { return }
                withAnimation(Theme.Motion.sweep) { phase = 1 }
            }
    }

    private var sweep: some View {
        GeometryReader { proxy in
            let travel = proxy.size.width + proxy.size.width * 0.6
            LinearGradient(
                colors: [.clear, Theme.Colors.primaryText.opacity(0.06), .clear],
                startPoint: .leading, endPoint: .trailing
            )
            .frame(width: max(24, proxy.size.width * 0.6))
            .offset(x: phase * travel)
        }
        .allowsHitTesting(false)
    }
}

/// A placeholder shaped like the rows this app actually draws: a title line, `lines` of secondary text, an
/// optional trailing status pill, and an optional progress bar — i.e. `TaskRow`, `ScheduleRow`,
/// `ExperienceRow`, `DeviceRow`. Matching the eventual layout is the whole point: content then settles into
/// the shape already on screen instead of replacing it.
public struct SaliSkeletonRow: View {
    private let lines: Int
    private let showsPill: Bool
    private let showsBar: Bool

    public init(lines: Int = 2, showsPill: Bool = true, showsBar: Bool = false) {
        self.lines = lines
        self.showsPill = showsPill
        self.showsBar = showsBar
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack(alignment: .top, spacing: Theme.Spacing.s) {
                SaliSkeleton(height: 15)
                if showsPill {
                    SaliSkeleton(width: 64, height: 18, radius: Theme.Radius.pill)
                }
            }
            if showsBar {
                SaliSkeleton(height: 4, radius: Theme.Radius.pill)
            }
            ForEach(0..<max(0, lines), id: \.self) { index in
                SaliSkeleton(height: 11)
                    .padding(.trailing, index == lines - 1 ? 96 : 0)
            }
        }
    }
}

/// The three-up count header Memory and Learning open with.
public struct SaliSkeletonStats: View {
    private let count: Int
    public init(count: Int = 3) { self.count = count }
    public var body: some View {
        HStack(spacing: Theme.Spacing.l) {
            ForEach(0..<max(1, count), id: \.self) { _ in
                VStack(spacing: Theme.Spacing.s) {
                    SaliSkeleton(width: 44, height: 24)
                    SaliSkeleton(width: 68, height: 10)
                }
                .frame(maxWidth: .infinity)
            }
        }
    }
}

/// A `SaliSkeletonRow` wearing the same card as the real content (`saliCard`), so a grid or stack of these
/// occupies exactly the geometry the loaded screen will.
public struct SaliSkeletonCard: View {
    private let lines: Int
    private let showsPill: Bool
    private let showsBar: Bool

    public init(lines: Int = 2, showsPill: Bool = true, showsBar: Bool = false) {
        self.lines = lines
        self.showsPill = showsPill
        self.showsBar = showsBar
    }

    public var body: some View {
        SaliSkeletonRow(lines: lines, showsPill: showsPill, showsBar: showsBar)
            .frame(maxWidth: .infinity, alignment: .leading)
            .saliCard()
    }
}

/// The canonical FIRST-LOAD state for a list screen: the Theme canvas with card-shaped placeholders where
/// the rows are about to be. Replaces the bare centered spinner that used to hard-swap into full content.
///
/// Only ever shown when nothing is loaded yet — a pull-to-refresh or a background poll must leave the
/// loaded list on screen (items 2, 3, 4), never fall back here.
public struct SaliSkeletonList: View {
    private let rows: Int
    private let showsStats: Bool
    private let showsPill: Bool
    private let showsBar: Bool
    private let lines: Int

    public init(rows: Int = 5, showsStats: Bool = false, showsPill: Bool = true,
                showsBar: Bool = false, lines: Int = 2) {
        self.rows = rows
        self.showsStats = showsStats
        self.showsPill = showsPill
        self.showsBar = showsBar
        self.lines = lines
    }

    public var body: some View {
        ScrollView {
            VStack(spacing: Theme.Spacing.m) {
                if showsStats {
                    SaliSkeletonStats().saliCard()
                }
                ForEach(0..<max(1, rows), id: \.self) { _ in
                    SaliSkeletonCard(lines: lines, showsPill: showsPill, showsBar: showsBar)
                }
            }
            .padding(Theme.Spacing.l)
        }
        .scrollDisabled(true)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.Colors.background)
        .allowsHitTesting(false)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Loading")
    }
}
