import SwiftUI

/// The visual system (§37). One source of truth for spacing, radii, typography, motion, and semantic colors
/// so every screen feels intentional and coherent in both light and dark mode. Colors are defined against
/// the system semantic palette (which adapts to the viewer's appearance) plus a calm Sali accent.
public enum Theme {
    public enum Spacing {
        public static let xs: CGFloat = 4
        public static let s: CGFloat = 8
        public static let m: CGFloat = 12
        public static let l: CGFloat = 16
        public static let xl: CGFloat = 24
        public static let xxl: CGFloat = 32
    }

    public enum Radius {
        public static let s: CGFloat = 8
        public static let m: CGFloat = 12
        public static let l: CGFloat = 18
        public static let pill: CGFloat = 999
    }

    public enum Colors {
        public static let accent = Color(red: 0.36, green: 0.52, blue: 0.98)      // calm Sali blue
        public static let accentSoft = accent.opacity(0.14)
        public static let surface = Color(.secondarySystemBackground)
        public static let surfaceRaised = Color(.tertiarySystemBackground)
        public static let background = Color(.systemBackground)
        public static let primaryText = Color(.label)
        public static let secondaryText = Color(.secondaryLabel)
        public static let separator = Color(.separator)

        // Semantic status
        public static let ok = Color.green
        public static let warn = Color.orange
        public static let danger = Color.red
        public static let info = accent
        public static let idle = Color(.systemGray)
    }

    public enum Motion {
        public static let quick = Animation.easeOut(duration: 0.18)
        public static let standard = Animation.spring(response: 0.35, dampingFraction: 0.86)
        public static let gentle = Animation.easeInOut(duration: 0.4)
    }

    public enum Typography {
        public static let title = Font.system(.title2, design: .rounded).weight(.semibold)
        public static let heading = Font.system(.headline, design: .rounded)
        public static let body = Font.system(.body)
        public static let caption = Font.system(.caption)
        public static let mono = Font.system(.callout, design: .monospaced)
    }
}

public extension View {
    /// A standard raised surface card (§37 surface hierarchy).
    func saliCard(padding: CGFloat = Theme.Spacing.l) -> some View {
        self
            .padding(padding)
            .background(Theme.Colors.surface)
            .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.m, style: .continuous))
    }
}
