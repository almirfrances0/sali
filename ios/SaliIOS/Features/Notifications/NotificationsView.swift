import SwiftUI

/// "Messages from Sali" (§10/§11/§36) — the chronological record of things Sali brought to *you*, not
/// things you asked it. That's agent-initiated conversational messages (`agent.message`) plus the handful
/// of events important enough to surface here even without a chat bubble: a finished task, a resource
/// incident, an abandoned intent, or an error. Everything else in the live event stream (tool chatter,
/// step-by-step progress) belongs on the Activity tab, not here.
///
/// Pushed as a `NavigationLink` destination from `MoreView`, so this view does not open its own
/// `NavigationStack`.
struct NotificationsView: View {
    @EnvironmentObject private var appState: AppState

    /// Newest first. `liveEvents` is already bounded (§40) and append-ordered, so a reversed filter is
    /// enough — no need to re-sort by timestamp (which can be nil for a frame that arrived without one).
    private var items: [SaliEvent] {
        Array(appState.liveEvents.filter(Self.isRelevant).reversed())
    }

    var body: some View {
        Group {
            if items.isEmpty {
                EmptyStateView(
                    icon: "bell.slash",
                    title: "No messages yet",
                    message: """
                    When Sali reaches out on its own — finishing a task, flagging a problem, or needing your \
                    attention — it will show up here.
                    """
                )
            } else {
                List {
                    Section {
                        ForEach(items) { event in
                            NotificationRow(event: event)
                                .listRowSeparator(.hidden)
                                .listRowBackground(Color.clear)
                                .listRowInsets(EdgeInsets(
                                    top: Theme.Spacing.xs, leading: Theme.Spacing.l,
                                    bottom: Theme.Spacing.xs, trailing: Theme.Spacing.l
                                ))
                        }
                    } header: {
                        Text("Sali only shows up here on its own initiative — not for anything you asked for directly.")
                            .font(Theme.Typography.caption)
                            .foregroundStyle(Theme.Colors.secondaryText)
                            .textCase(nil)
                            .padding(.bottom, Theme.Spacing.xs)
                    }
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
                .background(Theme.Colors.background)
            }
        }
        .navigationTitle("Messages")
        .toolbar {
            // `.primaryAction` (not `.topBarTrailing`, which has no plain-macOS overload — Package.swift
            // also targets macOS for `swift test`) renders in the trailing nav position on iOS.
            ToolbarItem(placement: .primaryAction) {
                ConnectionBadge(appState.ws.state)
            }
        }
    }

    private static func isRelevant(_ event: SaliEvent) -> Bool {
        switch event.type {
        case .agentMessage, .taskCompleted, .resourceIncident, .intentRevoked, .error: true
        default: false
        }
    }
}

/// Severity used purely for the row's color/icon — never a claim about ground truth beyond what the event
/// itself carries.
private enum NotificationImportance {
    case critical, important, normal

    var color: Color {
        switch self {
        case .critical: Theme.Colors.danger
        case .important: Theme.Colors.warn
        case .normal: Theme.Colors.accent
        }
    }
}

private struct NotificationRow: View {
    let event: SaliEvent

    var body: some View {
        HStack(alignment: .top, spacing: Theme.Spacing.m) {
            ZStack {
                Circle().fill(importance.color.opacity(0.16)).frame(width: 32, height: 32)
                Image(systemName: icon)
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(importance.color)
            }
            .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                if isAgentMessage {
                    Text("SALI")
                        .font(Theme.Typography.caption.weight(.semibold))
                        .foregroundStyle(Theme.Colors.accent)
                }

                Text(event.humanSummary)
                    .font(Theme.Typography.body.weight(isAgentMessage ? .semibold : .regular))
                    .foregroundStyle(Theme.Colors.primaryText)
                    .fixedSize(horizontal: false, vertical: true)

                HStack(spacing: Theme.Spacing.s) {
                    if let origin = event.origin, !origin.isEmpty {
                        Text(origin)
                            .font(Theme.Typography.caption)
                            .foregroundStyle(Theme.Colors.secondaryText)
                            .lineLimit(1)
                    }
                    if let timestamp = event.timestamp {
                        Text(timestamp, style: .relative)
                            .font(Theme.Typography.caption)
                            .foregroundStyle(Theme.Colors.secondaryText)
                    }
                }
            }

            Spacer(minLength: 0)
        }
        .padding(Theme.Spacing.m)
        .background(isAgentMessage ? Theme.Colors.accentSoft : Theme.Colors.surface)
        .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.m, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.m, style: .continuous)
                .strokeBorder(isAgentMessage ? Theme.Colors.accent.opacity(0.35) : Color.clear, lineWidth: 1)
        )
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLabel)
    }

    // `EventType` (defined in Core/Realtime/SaliEvent.swift) declares only `Sendable`, not `Equatable`, so
    // `==` isn't available on it — pattern-matching works regardless, since case matching doesn't need it.
    private var isAgentMessage: Bool {
        if case .agentMessage = event.type { return true }
        return false
    }

    private var importance: NotificationImportance {
        switch event.type {
        case .error:
            return .critical
        case .resourceIncident:
            let severity = event.string("severity")?.lowercased()
            return (severity == "critical" || severity == "emergency") ? .critical : .important
        case .intentRevoked:
            return .important
        default:
            return .normal
        }
    }

    private var icon: String {
        switch event.type {
        case .agentMessage: "bubble.left.and.text.bubble.right.fill"
        case .taskCompleted: "checkmark.circle.fill"
        case .resourceIncident: "exclamationmark.triangle.fill"
        case .intentRevoked: "xmark.circle.fill"
        case .error: "exclamationmark.octagon.fill"
        default: "bell.fill"
        }
    }

    private var accessibilityLabel: String {
        var parts: [String] = []
        if isAgentMessage { parts.append("Message from Sali") }
        parts.append(event.humanSummary)
        if let origin = event.origin, !origin.isEmpty { parts.append("from \(origin)") }
        if let timestamp = event.timestamp {
            parts.append(timestamp.formatted(.relative(presentation: .named)))
        }
        return parts.joined(separator: ", ")
    }
}
