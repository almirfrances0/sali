import SwiftUI
import Foundation

// Reusable, state-complete building blocks (§37). Every list/screen uses these so loading / empty / error /
// offline all look intentional — a polished app stays coherent even when there's no data.

/// A status pill with a semantic color.
public struct StatusPill: View {
    let text: String
    let color: Color
    public init(_ text: String, color: Color) { self.text = text; self.color = color }
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

/// A small connection indicator (§30) — always honest about whether data is live.
public struct ConnectionBadge: View {
    let state: ConnectionState
    public init(_ state: ConnectionState) { self.state = state }
    public var body: some View {
        HStack(spacing: Theme.Spacing.xs) {
            Circle().fill(color).frame(width: 8, height: 8)
                .opacity(state.isLive ? 1 : 0.6)
            Text(state.label).font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Connection: \(state.label)")
    }
    private var color: Color {
        switch state {
        case .connected: Theme.Colors.ok
        case .connecting, .reconnecting: Theme.Colors.warn
        case .offline: Theme.Colors.danger
        case .idle: Theme.Colors.idle
        }
    }
}

/// Section header used across screens.
public struct SectionHeader: View {
    let title: String
    let subtitle: String?
    public init(_ title: String, subtitle: String? = nil) { self.title = title; self.subtitle = subtitle }
    public var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(Theme.Typography.heading)
            if let subtitle { Text(subtitle).font(Theme.Typography.caption)
                .foregroundStyle(Theme.Colors.secondaryText) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// The canonical loading state.
public struct LoadingState: View {
    let message: String
    public init(_ message: String = "Loading…") { self.message = message }
    public var body: some View {
        VStack(spacing: Theme.Spacing.m) {
            ProgressView()
            Text(message).font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// The canonical empty state — coherent even when Sali has nothing to show.
public struct EmptyStateView: View {
    let icon: String
    let title: String
    let message: String
    public init(icon: String, title: String, message: String) {
        self.icon = icon; self.title = title; self.message = message
    }
    public var body: some View {
        VStack(spacing: Theme.Spacing.m) {
            Image(systemName: icon).font(.system(size: 40)).foregroundStyle(Theme.Colors.secondaryText)
            Text(title).font(Theme.Typography.heading)
            Text(message).font(Theme.Typography.body).foregroundStyle(Theme.Colors.secondaryText)
                .multilineTextAlignment(.center)
        }
        .padding(Theme.Spacing.xl)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// The canonical error / offline state with a retry.
public struct ErrorStateView: View {
    let message: String
    let retry: (() -> Void)?
    public init(_ message: String, retry: (() -> Void)? = nil) { self.message = message; self.retry = retry }
    public var body: some View {
        VStack(spacing: Theme.Spacing.m) {
            Image(systemName: "wifi.exclamationmark").font(.system(size: 36))
                .foregroundStyle(Theme.Colors.warn)
            Text(message).font(Theme.Typography.body).foregroundStyle(Theme.Colors.secondaryText)
                .multilineTextAlignment(.center)
            if let retry {
                Button("Try again", action: retry).buttonStyle(.borderedProminent).tint(Theme.Colors.accent)
            }
        }
        .padding(Theme.Spacing.xl)
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
        case .idle, .loading: LoadingState()
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
                Capsule().fill(Theme.Colors.separator.opacity(0.35))
                Capsule().fill(tint).frame(width: max(3, proxy.size.width * value))
            }
        }
        .frame(height: 6)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Confidence")
        .accessibilityValue("\(Int((value * 100).rounded())) percent")
    }
    private var tint: Color {
        switch value {
        case ..<0.34: Theme.Colors.warn
        case ..<0.67: Theme.Colors.info
        default: Theme.Colors.ok
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
                    .font(.system(size: 32))
                    .foregroundStyle(isDestructive ? Theme.Colors.danger : Theme.Colors.accent)
                Text(explanation)
                    .font(Theme.Typography.body)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                Button(role: isDestructive ? .destructive : nil) {
                    onConfirm()
                    dismiss()
                } label: {
                    Text(confirmLabel).frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .tint(isDestructive ? Theme.Colors.danger : Theme.Colors.accent)
                .disabled(confirmDisabled)
                Button("Cancel", role: .cancel) { dismiss() }
                    .frame(maxWidth: .infinity)
            }
            .padding(Theme.Spacing.xl)
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
        }
        .presentationDetents([.medium])
    }
}

public extension Date {
    /// A short, natural relative description ("2 hours ago") — used wherever the design calls for a
    /// freshness cue rather than a raw timestamp.
    var saliRelative: String {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .short
        return f.localizedString(for: self, relativeTo: Date())
    }
}
