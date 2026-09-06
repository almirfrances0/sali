import SwiftUI

/// The Sali identity mark — deliberately not a robot, brain, sparkle, or chat bubble.
///
/// A single continuous ring with one small aperture, around a solid core: *one presence, one mind,
/// continuity, openness.* Pure vector, pure monochrome — it renders in a single `color` (ink by default),
/// so it adapts to light/dark automatically and works at every size: app icon, nav, avatar, loading,
/// empty state. The `animated` variant breathes gently for thinking/streaming — calm, never busy.
///
/// Its two loops are the app's ambient tempo made visible: the core breathes on `Theme.Motion.breathing`
/// (one beat) and the ring turns on `Theme.Motion.orbit` (four beats). Because both are whole multiples of
/// the same beat they stay in phase with each other *and* with every other ambient loop on screen —
/// previously the mark ran 1.4s against 6.0s, which drifted, and drifting loops read as nervous.
public struct SaliMark: View {
    public var size: CGFloat
    public var color: Color
    public var animated: Bool

    public init(size: CGFloat = 28, color: Color = Theme.Colors.accent, animated: Bool = false) {
        self.size = size
        self.color = color
        self.animated = animated
    }

    @State private var spin = false
    @State private var breathe = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var ringWidth: CGFloat { max(1.5, size * 0.085) }
    private var coreScale: CGFloat { 0.30 }

    public var body: some View {
        ZStack {
            // Continuous ring with one small aperture at the upper-right.
            Circle()
                .trim(from: 0.045, to: 0.955)
                .stroke(color, style: StrokeStyle(lineWidth: ringWidth, lineCap: .round))
                .rotationEffect(.degrees(-58))
                .padding(ringWidth / 2)
                .rotationEffect(.degrees(animated && spin ? 360 : 0))

            // The core — one mind.
            Circle()
                .fill(color)
                .frame(width: size * coreScale, height: size * coreScale)
                .scaleEffect(animated && breathe ? 1.14 : 1.0)
                .opacity(animated && breathe ? 1.0 : 0.9)
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
        .onAppear { startIfNeeded() }
        .onChange(of: animated) { _, _ in startIfNeeded() }
    }

    private func startIfNeeded() {
        // The ambient reduce-motion pattern: bail out *before* touching the driving state. A
        // `repeatForever` animation gated to `nil` would still set `spin`/`breathe`, and the mark would
        // silently snap to the end pose and hold it.
        guard animated, !reduceMotion else { spin = false; breathe = false; return }
        withAnimation(Theme.Motion.orbit) { spin = true }
        withAnimation(Theme.Motion.breathing) { breathe = true }
    }
}

/// The mark set inside a filled ink lozenge — the "avatar" treatment used beside Sali's messages and
/// anywhere a contained presence reads better than a bare glyph.
public struct SaliAvatar: View {
    public var size: CGFloat
    public var animated: Bool
    public init(size: CGFloat = 30, animated: Bool = false) {
        self.size = size; self.animated = animated
    }
    public var body: some View {
        // The lozenge radius is proportional rather than a `Theme.Radius` step on purpose: this container
        // is sized by its glyph, from 20pt to 64pt, and a fixed radius would read as a circle at one end of
        // that range and a square at the other. It is the one sanctioned exception to the radius ramp.
        SaliMark(size: size * 0.62, color: Theme.Colors.onAccent, animated: animated)
            .frame(width: size, height: size)
            .background(Theme.Colors.accent)
            .clipShape(RoundedRectangle(cornerRadius: size * 0.32, style: .continuous))
    }
}

#Preview {
    VStack(spacing: 24) {
        HStack(spacing: 24) {
            SaliMark(size: 64)
            SaliMark(size: 64, animated: true)
            SaliAvatar(size: 64)
        }
        HStack(spacing: 24) {
            SaliMark(size: 28)
            SaliMark(size: 20)
            SaliAvatar(size: 30, animated: true)
        }
    }
    .padding(40)
    .frame(maxWidth: .infinity, maxHeight: .infinity)
    .background(Theme.Colors.background)
}
