import SwiftUI

// Task management (Prompt 13, §14/§15) — a tab root, so this owns its own `NavigationStack`. Active tasks
// come from `GET /api/v1/tasks` (which never includes abandoned tasks server-side); a separate "Historical /
// abandoned" section is sourced from `GET /api/v1/intent/revoked` and is never presented as active (§15).

/// One tombstoned intent, as returned by `GET /api/v1/intent/revoked`.
struct RevokedTask: Decodable, Identifiable, Sendable {
    var id: String { taskId }
    let taskId: String
    let objective: String
    let reason: String?
    let revokedAt: Date?
    let supersededBy: String?

    enum CodingKeys: String, CodingKey {
        case taskId = "task_id", objective, reason, revokedAt = "revoked_at", supersededBy = "superseded_by"
    }
}

private struct RevokedTasksResponse: Decodable, Sendable { let revoked: [RevokedTask] }

/// Semantic color for a task's lifecycle state — shared across Activity, Tasks, and Task Detail.
extension TaskState {
    var color: Color {
        switch self {
        case .open, .running: Theme.Colors.info
        case .paused, .waitingForUser, .blocked: Theme.Colors.warn
        case .done: Theme.Colors.ok
        case .failed: Theme.Colors.danger
        case .abandoned, .cancelled, .superseded, .other: Theme.Colors.idle
        }
    }
}

@MainActor
final class TasksViewModel: ObservableObject {
    enum TaskFilter: String, CaseIterable, Identifiable, Hashable {
        case active = "Active"
        case all = "All"
        var id: String { rawValue }
    }

    @Published var tasksState: Loadable<TaskListResponse> = .idle
    @Published var filter: TaskFilter = .active

    @Published private(set) var revokedTasks: [RevokedTask] = []
    @Published private(set) var revokedLoadFailed = false

    @Published var revivingTaskId: String?
    @Published var reviveError: String?

    func loadAll(api: APIClient) async {
        async let tasksLoad: () = loadTasks(api: api)
        async let revokedLoad: () = loadRevoked(api: api)
        _ = await (tasksLoad, revokedLoad)
    }

    func loadTasks(api: APIClient) async {
        if case .loaded = tasksState {} else { tasksState = .loading }
        do {
            let response: TaskListResponse = try await api.get("tasks")
            tasksState = .loaded(response)
        } catch {
            if tasksState.value == nil {
                tasksState = .failed((error as? APIError)?.errorDescription ?? "Couldn't load tasks.")
            }
        }
    }

    func loadRevoked(api: APIClient) async {
        do {
            let response: RevokedTasksResponse = try await api.get("intent/revoked")
            revokedTasks = response.revoked
            revokedLoadFailed = false
        } catch {
            // Secondary, informational section — a failure here never blocks active task management.
            revokedLoadFailed = true
        }
    }

    func revive(taskId: String, api: APIClient) async {
        revivingTaskId = taskId
        defer { revivingTaskId = nil }
        do {
            try await api.postVoid("tasks/\(taskId)/revive")
            await loadAll(api: api)
        } catch {
            reviveError = (error as? APIError)?.errorDescription ?? "Couldn't revive that task."
        }
    }
}

struct TasksView: View {
    @EnvironmentObject private var appState: AppState
    @StateObject private var viewModel = TasksViewModel()

    private var allTasks: [TaskSummary] { viewModel.tasksState.value?.tasks ?? [] }

    private var filteredTasks: [TaskSummary] {
        let tasks = viewModel.filter == .active ? allTasks.filter { $0.state.isActive } : allTasks
        return tasks.sorted { ($0.updatedAt ?? .distantPast) > ($1.updatedAt ?? .distantPast) }
    }

    var body: some View {
        NavigationStack {
            Group {
                if viewModel.tasksState.value == nil {
                    switch viewModel.tasksState {
                    case .failed(let message):
                        ErrorStateView(message) { Task { await viewModel.loadAll(api: appState.api) } }
                    default:
                        LoadingState("Loading tasks…")
                    }
                } else {
                    taskList
                }
            }
            .navigationTitle("Tasks")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    ConnectionBadge(appState.ws.state)
                }
            }
        }
        .task { await viewModel.loadAll(api: appState.api) }
        .alert("Couldn't revive that task", isPresented: reviveErrorBinding) {
            Button("OK", role: .cancel) { viewModel.reviveError = nil }
        } message: {
            Text(viewModel.reviveError ?? "")
        }
    }

    private var reviveErrorBinding: Binding<Bool> {
        Binding(get: { viewModel.reviveError != nil }, set: { if !$0 { viewModel.reviveError = nil } })
    }

    // MARK: - Loaded content

    private var taskList: some View {
        List {
            Section {
                Picker("Filter", selection: $viewModel.filter) {
                    ForEach(TasksViewModel.TaskFilter.allCases) { filter in
                        Text(filter.rawValue).tag(filter)
                    }
                }
                .pickerStyle(.segmented)
                .accessibilityLabel("Task filter")
            }
            .listRowSeparator(.hidden)
            .listRowBackground(Color.clear)

            Section {
                if filteredTasks.isEmpty {
                    Text(viewModel.filter == .active ? "No active tasks right now." : "No tasks recorded yet.")
                        .font(Theme.Typography.body)
                        .foregroundStyle(Theme.Colors.secondaryText)
                } else {
                    ForEach(filteredTasks) { task in
                        NavigationLink {
                            TaskDetailView(taskId: task.id)
                        } label: {
                            TaskRow(task: task, isPrimary: task.id == viewModel.tasksState.value?.activeTaskId)
                        }
                    }
                }
            } header: {
                SectionHeader(viewModel.filter == .active ? "Active" : "All tasks")
            }

            if !viewModel.revokedTasks.isEmpty || viewModel.revokedLoadFailed {
                Section {
                    if viewModel.revokedLoadFailed && viewModel.revokedTasks.isEmpty {
                        HStack {
                            Text("Couldn't load historical tasks.")
                                .font(Theme.Typography.caption)
                                .foregroundStyle(Theme.Colors.secondaryText)
                            Spacer()
                            Button("Try again") { Task { await viewModel.loadRevoked(api: appState.api) } }
                                .font(Theme.Typography.caption)
                        }
                    } else {
                        ForEach(viewModel.revokedTasks) { item in
                            RevokedTaskRow(
                                item: item,
                                canControl: appState.role.canControl,
                                isReviving: viewModel.revivingTaskId == item.taskId
                            ) {
                                Task { await viewModel.revive(taskId: item.taskId, api: appState.api) }
                            }
                        }
                    }
                } header: {
                    SectionHeader("Historical / abandoned",
                                  subtitle: "Not currently active — will not auto-resume")
                }
            }
        }
        .listStyle(.insetGrouped)
        .refreshable { await viewModel.loadAll(api: appState.api) }
    }
}

// MARK: - Rows

private struct TaskRow: View {
    let task: TaskSummary
    let isPrimary: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack(alignment: .top, spacing: Theme.Spacing.s) {
                Text(task.objective)
                    .font(Theme.Typography.body.weight(.medium))
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: Theme.Spacing.s)
                StatusPill(task.state.label, color: task.state.color)
            }

            ProgressView(value: task.progress)
                .tint(task.state.color)
                .accessibilityLabel("Progress")
                .accessibilityValue("\(Int((task.progress * 100).rounded())) percent")

            HStack(spacing: Theme.Spacing.s) {
                if isPrimary {
                    Label("Primary", systemImage: "star.fill")
                }
                if let root = task.workspaceRoot, !root.isEmpty {
                    Label(URL(fileURLWithPath: root).lastPathComponent, systemImage: "folder")
                        .lineLimit(1)
                }
                Spacer()
                if let updatedAt = task.updatedAt {
                    Text("Updated \(updatedAt.saliRelative)")
                }
            }
            .font(Theme.Typography.caption)
            .foregroundStyle(Theme.Colors.secondaryText)
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }
}

private struct RevokedTaskRow: View {
    let item: RevokedTask
    let canControl: Bool
    let isReviving: Bool
    let onRevive: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            Text(item.objective)
                .font(Theme.Typography.body.weight(.medium))
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)

            if let reason = item.reason, !reason.isEmpty {
                Text(reason)
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }

            HStack(alignment: .center, spacing: Theme.Spacing.s) {
                Label("Not currently active — will not auto-resume", systemImage: "moon.zzz.fill")
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .lineLimit(2)
                Spacer()
                if canControl {
                    if isReviving {
                        ProgressView()
                    } else {
                        Button("Revive", action: onRevive)
                            .buttonStyle(.bordered)
                    }
                }
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }
}

#Preview {
    TasksView().environmentObject(AppState())
}
