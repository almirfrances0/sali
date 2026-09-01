import SwiftUI

// Full task inspection (Prompt 13, §14/§42.5) — pushed from `TasksView`, so (like `NotificationsView`) this
// does not open its own `NavigationStack`. The core `TaskResponse` anchors the screen; workspace/skills/
// research/reviews are supplementary sections that degrade gracefully on their own — a hiccup fetching
// "reviews" never blanks the steps timeline or the controls.

/// `GET /api/v1/tasks/{id}/workspace` — authoritative workspace roots + mode. Every field optional: an
/// unrecognized or partial shape still decodes to "nothing recorded" rather than throwing away the screen.
struct WorkspaceDetail: Decodable, Sendable {
    let workspaceRoot: String?
    let workspaceMode: String?
    let roots: [String]?

    enum CodingKeys: String, CodingKey {
        case workspaceRoot = "workspace_root", workspaceMode = "workspace_mode", roots
    }
}

/// `GET /api/v1/tasks/{id}/skills` → `{skills:[{name,path,content_hash,score}]}`.
private struct SkillsResponse: Decodable, Sendable { let skills: [SkillSnapshot] }

struct SkillSnapshot: Decodable, Identifiable, Sendable {
    var id: String { path }
    let name: String
    let path: String
    let contentHash: String?
    let score: Double?

    enum CodingKeys: String, CodingKey { case name, path, contentHash = "content_hash", score }
}

/// `GET /api/v1/tasks/{id}/research` → `{research:[...], candidates:[...]}`. Item shapes aren't pinned down
/// by the API reference beyond "counts + brief", so every field here is optional and a sensible fallback
/// chain produces a one-line brief without inventing content.
struct ResearchSummary: Decodable, Sendable {
    let research: [ResearchEntry]
    let candidates: [ResearchEntry]
}

struct ResearchEntry: Decodable, Identifiable, Sendable {
    var id: String { identifier ?? UUID().uuidString }
    let identifier: String?
    let summary: String?
    let title: String?
    let query: String?

    enum CodingKeys: String, CodingKey { case identifier = "id", summary, title, query }

    var brief: String { summary ?? title ?? query ?? "Untitled" }
}

/// `GET /api/v1/tasks/{id}/reviews` → `{attempts, reviewer_status, latest, reviews}`.
struct TaskReviewsResponse: Decodable, Sendable {
    let attempts: Int?
    let reviewerStatus: String?
    let latest: ReviewEntry?
    let reviews: [ReviewEntry]?

    enum CodingKeys: String, CodingKey {
        case attempts, reviewerStatus = "reviewer_status", latest, reviews
    }
}

struct ReviewEntry: Decodable, Identifiable, Sendable {
    // Each review attempt has a distinct `attempt` number → a stable, non-colliding id.
    var id: String { "attempt-\(sequence ?? 0)" }
    let sequence: Int?
    let verdict: String?
    let reason: String?
    let createdAt: Date?

    // The backend (TaskReviewer.to_public, src/sali/tasks/reviewer.py) sends `attempt`, `status`, `summary`
    // — NOT sequence/verdict/reason. Map to the real keys; `created_at` isn't sent, so it stays nil.
    enum CodingKeys: String, CodingKey {
        case sequence = "attempt", verdict = "status", reason = "summary", createdAt = "created_at"
    }
}

@MainActor
final class TaskDetailViewModel: ObservableObject {
    let taskId: String

    @Published var task: Loadable<TaskSummary> = .idle
    @Published private(set) var workspace: WorkspaceDetail?
    @Published private(set) var skills: [SkillSnapshot] = []
    @Published private(set) var research: ResearchSummary?
    @Published private(set) var reviews: TaskReviewsResponse?
    @Published private(set) var resources: ResourceSnapshot?
    @Published var artifacts: Loadable<[ArtifactMeta]> = .idle

    @Published var isPerformingControl = false
    @Published var controlError: String?

    @Published private(set) var downloadingArtifactId: String?
    @Published private(set) var downloadedFiles: [String: URL] = [:]
    @Published var downloadError: String?

    init(taskId: String) { self.taskId = taskId }

    func loadAll(api: APIClient) async {
        async let taskLoad: () = loadTask(api: api)
        async let workspaceLoad: () = loadWorkspace(api: api)
        async let skillsLoad: () = loadSkills(api: api)
        async let researchLoad: () = loadResearch(api: api)
        async let reviewsLoad: () = loadReviews(api: api)
        async let resourcesLoad: () = loadResources(api: api)
        async let artifactsLoad: () = loadArtifacts(api: api)
        _ = await (taskLoad, workspaceLoad, skillsLoad, researchLoad, reviewsLoad, resourcesLoad, artifactsLoad)
    }

    private func loadTask(api: APIClient) async {
        if case .loaded = task {} else { task = .loading }
        do {
            let value: TaskSummary = try await api.get("tasks/\(taskId)")
            task = .loaded(value)
        } catch {
            task = .failed((error as? APIError)?.errorDescription ?? "Couldn't load this task.")
        }
    }

    private func loadWorkspace(api: APIClient) async {
        workspace = try? await api.get("tasks/\(taskId)/workspace")
    }

    private func loadSkills(api: APIClient) async {
        let response: SkillsResponse? = try? await api.get("tasks/\(taskId)/skills")
        skills = response?.skills ?? []
    }

    private func loadResearch(api: APIClient) async {
        research = try? await api.get("tasks/\(taskId)/research")
    }

    private func loadReviews(api: APIClient) async {
        reviews = try? await api.get("tasks/\(taskId)/reviews")
    }

    /// Best-effort — connects "why is this slow/paused" to the same resource ladder the System screen shows,
    /// without inventing a per-task field the backend doesn't have (§13 "connect metrics to meaning").
    private func loadResources(api: APIClient) async {
        resources = try? await api.get("resources")
    }

    func loadArtifacts(api: APIClient) async {
        if case .loaded = artifacts {} else { artifacts = .loading }
        do {
            let list: [ArtifactMeta] = try await api.get("tasks/\(taskId)/artifacts")
            artifacts = .loaded(list)
        } catch {
            artifacts = .failed((error as? APIError)?.errorDescription ?? "Couldn't load artifacts.")
        }
    }

    // MARK: - Controls (owner/controller only, gated in the view)

    func pause(api: APIClient) async { await control(api: api, action: "pause") }
    func resume(api: APIClient) async { await control(api: api, action: "resume") }

    @discardableResult
    func abandon(api: APIClient) async -> Bool { await control(api: api, action: "abandon") }

    @discardableResult
    private func control(api: APIClient, action: String) async -> Bool {
        isPerformingControl = true
        defer { isPerformingControl = false }
        do {
            try await api.postVoid("tasks/\(taskId)/\(action)")
            if action != "abandon" { await loadTask(api: api) }
            return true
        } catch {
            controlError = (error as? APIError)?.errorDescription ?? "Couldn't complete that action."
            return false
        }
    }

    // MARK: - Artifact download → ShareLink

    /// Downloads the artifact's bytes and writes them into the app's own sandbox temp directory, keyed only
    /// by its own filename — the server-recorded host path is never requested or displayed (§9/§43).
    func download(_ artifact: ArtifactMeta, api: APIClient) async {
        downloadingArtifactId = artifact.id
        defer { downloadingArtifactId = nil }
        do {
            let (data, _) = try await api.download(relativePath: artifact.downloadURL)
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let fileURL = directory.appendingPathComponent(artifact.filename)
            try data.write(to: fileURL, options: .atomic)
            downloadedFiles[artifact.id] = fileURL
        } catch {
            downloadError = (error as? APIError)?.errorDescription ?? "Couldn't download that file."
        }
    }
}

struct TaskDetailView: View {
    let taskId: String
    @EnvironmentObject private var appState: AppState
    @StateObject private var viewModel: TaskDetailViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var showAbandonSheet = false

    init(taskId: String) {
        self.taskId = taskId
        _viewModel = StateObject(wrappedValue: TaskDetailViewModel(taskId: taskId))
    }

    var body: some View {
        Group {
            switch viewModel.task {
            case .idle, .loading:
                LoadingState("Loading task…")
            case .failed(let message):
                ErrorStateView(message) { Task { await viewModel.loadAll(api: appState.api) } }
            case .loaded(let task):
                detail(for: task)
            }
        }
        .navigationTitle("Task")
        .navigationBarTitleDisplayMode(.inline)
        .task { await viewModel.loadAll(api: appState.api) }
        .refreshable { await viewModel.loadAll(api: appState.api) }
        .alert("Couldn't complete that", isPresented: controlErrorBinding) {
            Button("OK", role: .cancel) { viewModel.controlError = nil }
        } message: {
            Text(viewModel.controlError ?? "")
        }
        .alert("Download failed", isPresented: downloadErrorBinding) {
            Button("OK", role: .cancel) { viewModel.downloadError = nil }
        } message: {
            Text(viewModel.downloadError ?? "")
        }
        .sheet(isPresented: $showAbandonSheet) {
            NaturalConfirmationSheet(
                title: "Abandon this task",
                explanation: """
                “\(taskObjective)” will be tombstoned as an abandoned intent. Sali records this as a closed \
                chapter — any steps still depending on it are cancelled with it, and it won't auto-resume on \
                its own. You'll still be able to find it under Tasks → Historical, where Revive starts a new \
                task from the same objective whenever you're ready.
                """,
                confirmLabel: "Abandon task",
                isDestructive: true
            ) {
                Task {
                    if await viewModel.abandon(api: appState.api) {
                        dismiss()
                    }
                }
            }
        }
    }

    private var taskObjective: String { viewModel.task.value?.objective ?? "this task" }

    private var controlErrorBinding: Binding<Bool> {
        Binding(get: { viewModel.controlError != nil }, set: { if !$0 { viewModel.controlError = nil } })
    }
    private var downloadErrorBinding: Binding<Bool> {
        Binding(get: { viewModel.downloadError != nil }, set: { if !$0 { viewModel.downloadError = nil } })
    }

    // MARK: - Loaded content

    @ViewBuilder
    private func detail(for task: TaskSummary) -> some View {
        List {
            Section {
                overviewCard(task)
            }
            .listRowSeparator(.hidden)
            .listRowBackground(Color.clear)

            if appState.role.canControl && task.state.isActive {
                Section {
                    controlsRow(for: task)
                }
            }

            if let resources = viewModel.resources, let state = resources.state {
                Section {
                    HStack(spacing: Theme.Spacing.s) {
                        StatusPill(state.capitalized, color: resources.resourceState.color)
                        Text(resources.resourceState.meaning)
                            .font(Theme.Typography.caption)
                            .foregroundStyle(Theme.Colors.secondaryText)
                    }
                } header: {
                    SectionHeader("Host resources")
                }
            }

            Section {
                if task.steps.isEmpty {
                    Text("No steps recorded for this task.")
                        .font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.secondaryText)
                } else {
                    ForEach(task.steps) { step in
                        StepRow(step: step)
                    }
                }
            } header: {
                SectionHeader("Steps", subtitle: stepsSubtitle(task))
            }

            Section {
                workspaceContent
            } header: {
                SectionHeader("Workspace")
            }

            Section {
                skillsContent
            } header: {
                SectionHeader("Skills", subtitle: viewModel.skills.isEmpty ? nil : "\(viewModel.skills.count) selected")
            }

            Section {
                researchContent
            } header: {
                SectionHeader("Research")
            }

            Section {
                reviewsContent
            } header: {
                SectionHeader("Reviewer")
            }

            Section {
                artifactsContent
            } header: {
                SectionHeader("Artifacts", subtitle: artifactsSubtitle)
            }
        }
        .listStyle(.insetGrouped)
    }

    private func stepsSubtitle(_ task: TaskSummary) -> String? {
        guard !task.steps.isEmpty else { return nil }
        let done = task.steps.filter { $0.status == "done" }.count
        return "\(done)/\(task.steps.count) complete"
    }

    private var artifactsSubtitle: String? {
        guard let list = viewModel.artifacts.value, !list.isEmpty else { return nil }
        return "\(list.count)"
    }

    private func overviewCard(_ task: TaskSummary) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            Text(task.objective)
                .font(Theme.Typography.title)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: Theme.Spacing.s) {
                StatusPill(task.state.label, color: task.state.color)
                if task.retryCount > 0 {
                    Text("Retry \(task.retryCount)/\(task.maxRetries)")
                        .font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.secondaryText)
                }
            }

            ProgressView(value: task.progress)
                .tint(task.state.color)
                .accessibilityLabel("Progress")
                .accessibilityValue("\(Int((task.progress * 100).rounded())) percent")

            if let result = task.result, !result.isEmpty {
                Text(result)
                    .font(Theme.Typography.body)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let updatedAt = task.updatedAt {
                Text("Updated \(updatedAt.saliRelative)")
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .saliCard()
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder
    private func controlsRow(for task: TaskSummary) -> some View {
        HStack(spacing: Theme.Spacing.m) {
            Button {
                Task { await viewModel.pause(api: appState.api) }
            } label: {
                Label("Pause", systemImage: "pause.fill")
            }
            .disabled(viewModel.isPerformingControl || task.state != .running)

            Button {
                Task { await viewModel.resume(api: appState.api) }
            } label: {
                Label("Resume", systemImage: "play.fill")
            }
            .disabled(viewModel.isPerformingControl || task.state != .paused)

            Spacer()

            if viewModel.isPerformingControl {
                ProgressView()
            }

            Button(role: .destructive) {
                showAbandonSheet = true
            } label: {
                Label("Abandon", systemImage: "xmark.circle")
            }
            .disabled(viewModel.isPerformingControl)
        }
        .buttonStyle(.bordered)
    }

    // MARK: - Workspace

    @ViewBuilder
    private var workspaceContent: some View {
        if let workspace = viewModel.workspace,
           (workspace.workspaceRoot?.isEmpty == false || workspace.workspaceMode?.isEmpty == false) {
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                if let root = workspace.workspaceRoot, !root.isEmpty {
                    Label(URL(fileURLWithPath: root).lastPathComponent, systemImage: "folder.fill")
                        .font(Theme.Typography.body)
                }
                if let mode = workspace.workspaceMode, !mode.isEmpty {
                    Text("Mode: \(mode)")
                        .font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.secondaryText)
                }
                if let roots = workspace.roots, roots.count > 1 {
                    Text("\(roots.count) linked locations")
                        .font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.secondaryText)
                }
            }
        } else {
            Text("No workspace recorded for this task.")
                .font(Theme.Typography.caption)
                .foregroundStyle(Theme.Colors.secondaryText)
        }
    }

    // MARK: - Skills

    @ViewBuilder
    private var skillsContent: some View {
        if viewModel.skills.isEmpty {
            Text("No skills recorded for this task.")
                .font(Theme.Typography.caption)
                .foregroundStyle(Theme.Colors.secondaryText)
        } else {
            ForEach(viewModel.skills) { skill in
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(skill.name).font(Theme.Typography.body)
                        Text(skill.path)
                            .font(Theme.Typography.mono)
                            .foregroundStyle(Theme.Colors.secondaryText)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                    Spacer()
                    if let score = skill.score {
                        Text("\(Int((score * 100).rounded()))%")
                            .font(Theme.Typography.caption)
                            .foregroundStyle(Theme.Colors.secondaryText)
                    }
                }
                .accessibilityElement(children: .combine)
            }
        }
    }

    // MARK: - Research

    @ViewBuilder
    private var researchContent: some View {
        if let research = viewModel.research, !(research.research.isEmpty && research.candidates.isEmpty) {
            VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                HStack(spacing: Theme.Spacing.l) {
                    Label("\(research.research.count) findings", systemImage: "doc.text.magnifyingglass")
                    Label("\(research.candidates.count) learning candidates", systemImage: "lightbulb")
                }
                .font(Theme.Typography.caption)
                .foregroundStyle(Theme.Colors.secondaryText)

                ForEach(research.research.prefix(3)) { entry in
                    Text(entry.brief)
                        .font(Theme.Typography.caption)
                        .lineLimit(2)
                }
            }
        } else {
            Text("No research recorded for this task.")
                .font(Theme.Typography.caption)
                .foregroundStyle(Theme.Colors.secondaryText)
        }
    }

    // MARK: - Reviewer

    @ViewBuilder
    private var reviewsContent: some View {
        if let reviews = viewModel.reviews, reviews.reviewerStatus != nil || reviews.attempts != nil {
            VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                HStack(spacing: Theme.Spacing.s) {
                    if let status = reviews.reviewerStatus {
                        StatusPill(status.replacingOccurrences(of: "_", with: " ").capitalized,
                                  color: reviewColor(status))
                    }
                    if let attempts = reviews.attempts {
                        Text("\(attempts) attempt\(attempts == 1 ? "" : "s")")
                            .font(Theme.Typography.caption)
                            .foregroundStyle(Theme.Colors.secondaryText)
                    }
                }
                if let reason = reviews.latest?.reason, !reason.isEmpty {
                    Text(reason)
                        .font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.secondaryText)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let history = reviews.reviews, history.count > 1 {
                    DisclosureGroup("Review history (\(history.count))") {
                        ForEach(history) { entry in
                            HStack {
                                Text(entry.verdict?.replacingOccurrences(of: "_", with: " ").capitalized ?? "—")
                                Spacer()
                                if let createdAt = entry.createdAt {
                                    Text(createdAt.saliRelative)
                                }
                            }
                            .font(Theme.Typography.caption)
                            .foregroundStyle(Theme.Colors.secondaryText)
                        }
                    }
                    .font(Theme.Typography.caption)
                }
            }
        } else {
            Text("No reviewer verdict recorded yet.")
                .font(Theme.Typography.caption)
                .foregroundStyle(Theme.Colors.secondaryText)
        }
    }

    private func reviewColor(_ status: String) -> Color {
        switch status.uppercased() {
        case "PASS": Theme.Colors.ok
        case "NEEDS_REWORK": Theme.Colors.warn
        case "BLOCKED": Theme.Colors.danger
        default: Theme.Colors.idle
        }
    }

    // MARK: - Artifacts

    @ViewBuilder
    private var artifactsContent: some View {
        switch viewModel.artifacts {
        case .idle, .loading:
            ProgressView().frame(maxWidth: .infinity, alignment: .center)
        case .failed(let message):
            HStack {
                Text(message)
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
                Spacer()
                Button("Try again") { Task { await viewModel.loadArtifacts(api: appState.api) } }
                    .font(Theme.Typography.caption)
            }
        case .loaded(let list) where list.isEmpty:
            Text("No artifacts yet.")
                .font(Theme.Typography.caption)
                .foregroundStyle(Theme.Colors.secondaryText)
        case .loaded(let list):
            ForEach(list) { artifact in
                ArtifactRow(
                    artifact: artifact,
                    isDownloading: viewModel.downloadingArtifactId == artifact.id,
                    downloadedURL: viewModel.downloadedFiles[artifact.id]
                ) {
                    Task { await viewModel.download(artifact, api: appState.api) }
                }
            }
        }
    }
}

// MARK: - Step row

private struct StepRow: View {
    let step: TaskStep

    var body: some View {
        HStack(alignment: .top, spacing: Theme.Spacing.m) {
            icon.frame(width: 20).accessibilityHidden(true)

            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                Text(step.description)
                    .font(Theme.Typography.body)
                    .fixedSize(horizontal: false, vertical: true)
                if let note = step.note, !note.isEmpty {
                    Text(note)
                        .font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.secondaryText)
                }
                if let error = step.lastError, !error.isEmpty {
                    Text(error)
                        .font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.danger)
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack(spacing: Theme.Spacing.s) {
                    if step.attempts > 1 {
                        Text("Attempt \(step.attempts)")
                    }
                    if let failureClass = step.failureClass, !failureClass.isEmpty {
                        Text(failureClass)
                            .foregroundStyle(Theme.Colors.warn)
                    }
                }
                .font(Theme.Typography.caption)
                .foregroundStyle(Theme.Colors.secondaryText)
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder
    private var icon: some View {
        if step.verified {
            Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.Colors.ok)
        } else if step.status == "failed" {
            Image(systemName: "xmark.circle.fill").foregroundStyle(Theme.Colors.danger)
        } else if step.status == "done" {
            Image(systemName: "checkmark.circle").foregroundStyle(Theme.Colors.warn)
        } else if step.status == "running" {
            Image(systemName: "circle.dotted").foregroundStyle(Theme.Colors.info)
        } else {
            Image(systemName: "circle").foregroundStyle(Theme.Colors.idle)
        }
    }
}

// MARK: - Artifact row

private struct ArtifactRow: View {
    let artifact: ArtifactMeta
    let isDownloading: Bool
    let downloadedURL: URL?
    let onDownload: () -> Void

    var body: some View {
        HStack(spacing: Theme.Spacing.m) {
            Image(systemName: icon)
                .foregroundStyle(Theme.Colors.accent)
                .frame(width: 22)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 2) {
                Text(artifact.filename)
                    .font(Theme.Typography.body)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(detailLine)
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }

            Spacer()

            trailing
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }

    private var detailLine: String {
        var parts: [String] = []
        if let size = artifact.size {
            parts.append(ByteCountFormatter.string(fromByteCount: Int64(size), countStyle: .file))
        }
        parts.append(artifact.artifactType.capitalized)
        if !artifact.available { parts.append("Unavailable") }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder
    private var trailing: some View {
        if let downloadedURL {
            ShareLink(item: downloadedURL) {
                Image(systemName: "square.and.arrow.up")
            }
            .accessibilityLabel("Share \(artifact.filename)")
        } else if isDownloading {
            ProgressView()
        } else {
            Button(action: onDownload) {
                Image(systemName: "arrow.down.circle")
            }
            .disabled(!artifact.available)
            .accessibilityLabel("Download \(artifact.filename)")
        }
    }

    private var icon: String {
        if artifact.contentType.hasPrefix("image/") { return "photo" }
        if artifact.contentType.hasPrefix("video/") { return "video" }
        if artifact.contentType == "application/pdf" { return "doc.richtext" }
        if artifact.contentType.hasPrefix("text/") { return "doc.text" }
        return "doc"
    }
}

// MARK: - Resource level color (Task Detail only — the authoritative color/meaning still lives on the model)

private extension ResourceLevel {
    var color: Color {
        switch self {
        case .safe: Theme.Colors.ok
        case .elevated: Theme.Colors.info
        case .high: Theme.Colors.warn
        case .critical, .emergency: Theme.Colors.danger
        }
    }
}

#Preview {
    NavigationStack {
        TaskDetailView(taskId: "preview-task")
    }
    .environmentObject(AppState())
}
