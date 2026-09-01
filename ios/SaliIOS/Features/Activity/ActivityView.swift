import SwiftUI

// The real-time Activity Center (Prompt 13, §13) — answers "what is Sali doing right now?" A cognitive
// snapshot on top (from `GET /api/v1/cognitive`), a human-readable live feed below driven purely by
// `appState.liveEvents` (never a second polling loop, §40). Chain-of-thought is never surfaced — only
// `SaliEvent.humanSummary`, which is already high-level by construction (Core/Realtime).

/// `GET /api/v1/cognitive` (alias `/current-life`) — the durable, reconstructed "what is Sali doing right
/// now?" view. Every field is optional: a metric/field this endpoint doesn't have right now degrades to nil
/// rather than showing a guess (golden rule).
struct CognitiveSnapshot: Decodable, Equatable {
    let objective: String?
    let nextAction: String?
    let taskId: String?
    let workspace: String?
    let phase: String?
    let taskStatus: String?
    let researchCount: Int?

    enum CodingKeys: String, CodingKey {
        case objective
        case nextAction = "next_action"
        case taskId = "task_id"
        case workspace
        case phase
        case taskStatus = "task_status"
        case researchCount = "research_count"
    }
}

/// Human view (default) vs. technical details (§13) — task_id/run_id/sequence/timestamp/origin, for anyone
/// who wants the wire-level facts rather than the plain-language summary.
enum ActivityDetailMode: String, CaseIterable, Identifiable, Hashable {
    case human = "Human view"
    case technical = "Technical details"
    var id: String { rawValue }
}

/// Pulls out a `Loadable`'s current value regardless of state — used across screens so a section can keep
/// showing last-known content while a background refresh is in flight or has failed transiently.
extension Loadable {
    var value: T? {
        if case .loaded(let value) = self { return value }
        return nil
    }
}

@MainActor
final class ActivityViewModel: ObservableObject {
    @Published var snapshot: Loadable<CognitiveSnapshot> = .idle
    @Published var detailMode: ActivityDetailMode = .human

    /// Refreshes the cognitive snapshot. A transient failure keeps showing the last-known snapshot instead
    /// of blanking the card (§16 "keep last-known, retry with backoff") — only a genuinely first load shows
    /// the error state.
    func refresh(api: APIClient) async {
        let hadValue = snapshot.value != nil
        if !hadValue { snapshot = .loading }
        do {
            let value: CognitiveSnapshot = try await api.get("cognitive")
            snapshot = .loaded(value)
        } catch {
            if !hadValue {
                snapshot = .failed((error as? APIError)?.errorDescription ?? "Couldn't read Sali's current state.")
            }
        }
    }
}

struct ActivityView: View {
    @EnvironmentObject private var appState: AppState
    @StateObject private var viewModel = ActivityViewModel()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// Events that a person would actually recognize as "activity" — connection housekeeping
    /// (connected/subscribed/pong/other) and raw streaming deltas are noise here, not signal.
    private var feedEvents: [SaliEvent] {
        appState.liveEvents.reversed().filter { event in
            switch event.type {
            case .connected, .subscribed, .pong, .other, .messageDelta: return false
            default: return true
            }
        }
    }

    /// §13: a banner appears when a `task.waiting_for_user` event is the *latest* event for the active task
    /// — waiting is a property of that specific task, not a global freeze.
    private var isWaitingForClarification: Bool {
        guard let taskId = viewModel.snapshot.value?.taskId else { return false }
        return appState.liveEvents.filter { $0.taskId == taskId }.last?.type == .taskWaiting
    }

    var body: some View {
        NavigationStack {
            Group {
                if viewModel.snapshot.value == nil && appState.liveEvents.isEmpty {
                    switch viewModel.snapshot {
                    case .failed(let message):
                        ErrorStateView(message) { Task { await viewModel.refresh(api: appState.api) } }
                    default:
                        LoadingState("Reading Sali's current state…")
                    }
                } else {
                    activityList
                }
            }
            .navigationTitle("Activity")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    ConnectionBadge(appState.ws.state)
                }
            }
        }
        .task { await viewModel.refresh(api: appState.api) }
        .refreshable { await viewModel.refresh(api: appState.api) }
        .onChange(of: appState.latestEvent?.id) { _, _ in
            // Event-driven, not time-driven (§40): only re-pull the snapshot when something that changes
            // "what is Sali doing right now" actually happened, never on a timer.
            guard let type = appState.latestEvent?.type, Self.snapshotTriggers.contains(type) else { return }
            Task { await viewModel.refresh(api: appState.api) }
        }
        .animation(reduceMotion ? nil : Theme.Motion.gentle, value: isWaitingForClarification)
    }

    private static let snapshotTriggers: Set<EventType> = [
        .taskStarted, .taskProgress, .taskCompleted, .taskWaiting, .taskSuspended, .taskResumed, .intentRevoked,
    ]

    // MARK: - Loaded content

    private var activityList: some View {
        List {
            Section {
                snapshotCard
            }
            .listRowSeparator(.hidden)
            .listRowBackground(Color.clear)

            if isWaitingForClarification {
                Section {
                    waitingBanner
                }
                .listRowSeparator(.hidden)
                .listRowBackground(Color.clear)
            }

            Section {
                Picker("Detail level", selection: $viewModel.detailMode) {
                    ForEach(ActivityDetailMode.allCases) { mode in
                        Text(mode.rawValue).tag(mode)
                    }
                }
                .pickerStyle(.segmented)
                .accessibilityLabel("Feed detail level")
            }
            .listRowSeparator(.hidden)
            .listRowBackground(Color.clear)

            Section {
                if feedEvents.isEmpty {
                    Text("Nothing yet — Sali's activity will appear here as it happens.")
                        .font(Theme.Typography.body)
                        .foregroundStyle(Theme.Colors.secondaryText)
                } else {
                    ForEach(feedEvents) { event in
                        EventRow(event: event, showTechnical: viewModel.detailMode == .technical)
                    }
                }
            } header: {
                SectionHeader("Live feed", subtitle: feedEvents.isEmpty ? nil : "\(feedEvents.count) recent")
            }
        }
        .listStyle(.insetGrouped)
    }

    @ViewBuilder
    private var snapshotCard: some View {
        switch viewModel.snapshot {
        case .idle, .loading:
            HStack(spacing: Theme.Spacing.m) {
                ProgressView()
                Text("Reading Sali's current state…")
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }
            .saliCard()
        case .failed(let message):
            VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                Label(message, systemImage: "wifi.exclamationmark")
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.warn)
                Button("Try again") { Task { await viewModel.refresh(api: appState.api) } }
                    .font(Theme.Typography.caption)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .saliCard()
        case .loaded(let snapshot):
            loadedSnapshotCard(snapshot)
        }
    }

    private func loadedSnapshotCard(_ snapshot: CognitiveSnapshot) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.m) {
            SectionHeader("Right now", subtitle: snapshot.phase.map { "Phase: \($0.capitalized)" })

            if let objective = snapshot.objective, !objective.isEmpty {
                Text(objective)
                    .font(Theme.Typography.title)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("Nothing in progress right now.")
                    .font(Theme.Typography.body)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }

            if let next = snapshot.nextAction, !next.isEmpty {
                Label(next, systemImage: "arrow.turn.down.right")
                    .font(Theme.Typography.body)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }

            HStack(spacing: Theme.Spacing.s) {
                if let status = snapshot.taskStatus {
                    let state = TaskState(rawValue: status) ?? .other
                    StatusPill(state.label, color: state.color)
                }
                if let workspace = snapshot.workspace, !workspace.isEmpty {
                    Label(URL(fileURLWithPath: workspace).lastPathComponent, systemImage: "folder")
                        .font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.secondaryText)
                        .lineLimit(1)
                }
                if let count = snapshot.researchCount, count > 0 {
                    Label("\(count) research", systemImage: "magnifyingglass")
                        .font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.secondaryText)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .saliCard()
        .accessibilityElement(children: .combine)
    }

    private var waitingBanner: some View {
        Label("Sali is waiting for your clarification", systemImage: "questionmark.circle.fill")
            .font(Theme.Typography.body.weight(.medium))
            .foregroundStyle(Theme.Colors.warn)
            .padding(Theme.Spacing.m)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.Colors.warn.opacity(0.12))
            .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.m, style: .continuous))
            .accessibilityElement(children: .combine)
    }
}

// MARK: - Event row

private struct EventRow: View {
    let event: SaliEvent
    let showTechnical: Bool

    var body: some View {
        HStack(alignment: .top, spacing: Theme.Spacing.m) {
            Image(systemName: icon)
                .foregroundStyle(color)
                .frame(width: 22)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                Text(event.humanSummary)
                    .font(Theme.Typography.body)
                    .fixedSize(horizontal: false, vertical: true)

                if showTechnical {
                    Text(technicalLine)
                        .font(Theme.Typography.mono)
                        .foregroundStyle(Theme.Colors.secondaryText)
                        .lineLimit(2)
                }
            }

            Spacer(minLength: Theme.Spacing.s)

            if let timestamp = event.timestamp {
                Text(timestamp, style: .relative)
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLabel)
    }

    private var technicalLine: String {
        var parts: [String] = []
        if let taskId = event.taskId { parts.append("task \(taskId.prefix(8))") }
        if let runId = event.runId { parts.append("run \(runId.prefix(8))") }
        if let sequence = event.sequence { parts.append("seq \(sequence)") }
        if let origin = event.origin, !origin.isEmpty { parts.append(origin) }
        return parts.isEmpty ? event.rawType : parts.joined(separator: " · ")
    }

    private var accessibilityLabel: String {
        var label = event.humanSummary
        if let timestamp = event.timestamp { label += ", \(timestamp.saliRelative)" }
        if showTechnical { label += ". \(technicalLine)" }
        return label
    }

    private var icon: String {
        switch event.type {
        case .messageStarted, .messageDelta, .messageCompleted: "bubble.left.and.bubble.right.fill"
        case .toolStarted, .toolProgress, .toolCompleted: "wrench.and.screwdriver.fill"
        case .taskStarted, .taskResumed: "play.circle.fill"
        case .taskProgress: "arrow.triangle.2.circlepath"
        case .taskWaiting: "questionmark.circle.fill"
        case .taskCompleted: "checkmark.circle.fill"
        case .taskSuspended: "pause.circle.fill"
        case .researchStarted: "magnifyingglass"
        case .researchCompleted: "doc.text.magnifyingglass"
        case .agentMessage: "bell.badge.fill"
        case .resourceIncident: "exclamationmark.triangle.fill"
        case .resourceState: "gauge.medium"
        case .intentRevoked: "xmark.circle.fill"
        case .error: "exclamationmark.octagon.fill"
        case .connected, .subscribed, .pong, .other: "circle.dotted"
        }
    }

    private var color: Color {
        switch event.type {
        case .error, .resourceIncident: Theme.Colors.danger
        case .taskWaiting: Theme.Colors.warn
        case .taskCompleted, .researchCompleted: Theme.Colors.ok
        case .taskSuspended, .intentRevoked: Theme.Colors.idle
        case .agentMessage: Theme.Colors.accent
        default: Theme.Colors.info
        }
    }
}

#Preview {
    ActivityView().environmentObject(AppState())
}
