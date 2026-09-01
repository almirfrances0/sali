import Foundation
import SwiftUI

// Memory — what Sali remembers (§16/§40). This is deliberately NOT a raw database browser: it surfaces
// distilled *experiences* (the durable, evidence-backed record of what Sali did and learned), the counts
// behind them, and any open contradictions — every one shown with its provenance, confidence, and
// freshness rather than presented as flat fact (golden rule, CLAUDE.md). Destructive memory actions are
// not implemented as easy taps; see `ForgetExperienceSheet` below.

// MARK: - Wire shapes not yet modeled in DomainModels.swift (decoded forgivingly — see report)

/// `GET /api/v1/memory` → `{ counts: {...}, recent_experiences: [Experience-ish] }`.
private struct MemoryOverviewResponse: Decodable {
    let counts: MemoryCounts?
    let recentExperiences: [Experience]?
    enum CodingKeys: String, CodingKey { case counts, recentExperiences = "recent_experiences" }
}

private struct MemoryCounts: Decodable {
    let total: Int?
    let byLayer: [String: Int]?
    let experiences: Int?
    let openConflicts: Int?
    enum CodingKeys: String, CodingKey {
        case total, byLayer = "by_layer", experiences, openConflicts = "open_conflicts"
    }
}

private struct ExperiencesResponse: Decodable { let experiences: [Experience]? }

/// `GET /api/v1/memory/conflicts` → `{ conflicts: [...] }` — rows from the `contradiction` table
/// (src/sali/learning/experience.py: `conflicts()`). Never silently overwritten; shown for review.
private struct MemoryConflictsResponse: Decodable { let conflicts: [MemoryConflictItem]? }

private struct MemoryConflictItem: Decodable, Identifiable {
    let conflictId: String?
    let oldSource: String?
    let newSource: String?
    let resolution: String?
    let status: String?
    let createdAt: Date?
    var id: String { conflictId ?? UUID().uuidString }
    enum CodingKeys: String, CodingKey {
        case conflictId = "id", oldSource = "old_source", newSource = "new_source"
        case resolution, status, createdAt = "created_at"
    }
    var isOpen: Bool { (status ?? "open") == "open" }
}

/// `GET /api/v1/memory/{id}` — the "why do I believe this?" provenance object (§11). A different shape
/// from `Experience`, so it's modeled separately and fetched only for the memory the user opens.
private struct MemoryProvenance: Decodable {
    let content: String?
    let layer: String?
    let source: String?
    let confidence: Double?
    let evidenceCount: Int?
    let evidenceState: String?
    let verification: String?
    let evidence: [Item]?

    struct Item: Decodable, Identifiable {
        let source: String?
        let confidence: Double?
        let note: String?
        var id: String { "\(source ?? "")-\(note ?? "")-\(confidence ?? 0)" }
    }

    enum CodingKeys: String, CodingKey {
        case content, layer, source, confidence
        case evidenceCount = "evidence_count", evidenceState = "evidence_state", verification, evidence
    }
}

// MARK: - View model

@MainActor
final class MemoryViewModel: ObservableObject {
    @Published var state: Loadable<Void> = .idle
    @Published var counts: MemoryCounts?
    @Published var recentExperiences: [Experience] = []
    @Published var conflicts: [MemoryConflictItem] = []

    @Published var searchText: String = ""
    @Published var searchResults: [Experience] = []
    @Published var isSearching = false
    @Published var searchError: String?

    @Published var provenance: Loadable<Void> = .idle
    @Published var provenanceDetail: MemoryProvenance?

    private var searchTask: Task<Void, Never>?

    /// Debounced search — triggered on every keystroke, but only actually hits the network ~350ms after
    /// typing pauses so the field feels responsive without hammering the backend per character.
    func scheduleSearch(api: APIClient) {
        searchTask?.cancel()
        let query = searchText
        searchTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 350_000_000)
            guard !Task.isCancelled, let self, self.searchText == query else { return }
            await self.search(api: api)
        }
    }

    func load(api: APIClient) async {
        state = .loading
        do {
            async let overview: MemoryOverviewResponse = api.get("memory")
            async let conflictResp: MemoryConflictsResponse = api.get("memory/conflicts")
            let (ov, cf) = try await (overview, conflictResp)
            counts = ov.counts
            recentExperiences = ov.recentExperiences ?? []
            conflicts = cf.conflicts ?? []
            state = .loaded(())
        } catch {
            state = .failed(Self.message(for: error))
        }
    }

    func search(api: APIClient) async {
        let q = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { searchResults = []; searchError = nil; return }
        isSearching = true
        searchError = nil
        do {
            let resp: ExperiencesResponse = try await api.get("experiences", query: ["objective": q])
            searchResults = resp.experiences ?? []
        } catch {
            searchError = Self.message(for: error)
            searchResults = []
        }
        isSearching = false
    }

    func clearSearch() {
        searchText = ""
        searchResults = []
        searchError = nil
    }

    func loadProvenance(memoryId: String, api: APIClient) async {
        provenance = .loading
        provenanceDetail = nil
        guard !memoryId.isEmpty else {
            provenance = .failed("No durable record to trace yet.")
            return
        }
        do {
            let detail: MemoryProvenance = try await api.get("memory/\(memoryId)")
            provenanceDetail = detail
            provenance = .loaded(())
        } catch {
            // A freshly-minted or synthetic id may not resolve to a `memory` row (e.g. search results
            // whose id isn't a UUID the endpoint recognizes) — degrade quietly, the summary card already
            // shown covers the essentials.
            provenance = .failed("Detailed provenance isn't available for this entry.")
        }
    }

    private static func message(for error: Error) -> String {
        (error as? APIError)?.errorDescription ?? "Couldn't reach Sali."
    }
}

// MARK: - View

struct MemoryView: View {
    @EnvironmentObject var appState: AppState
    @StateObject private var viewModel = MemoryViewModel()
    @State private var selectedExperience: Experience?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    // Pushed as a NavigationLink destination from MoreView, so — like NotificationsView — this does not
    // open its own NavigationStack; it shares the one already on screen.
    var body: some View {
        content
            .navigationTitle("Memory")
            .task { if case .idle = viewModel.state { await viewModel.load(api: appState.api) } }
            .refreshable { await viewModel.load(api: appState.api) }
            .sheet(item: $selectedExperience) { experience in
                ExperienceDetailSheet(experience: experience, viewModel: viewModel, api: appState.api,
                                      canManage: appState.role.canControl)
            }
    }

    @ViewBuilder private var content: some View {
        switch viewModel.state {
        case .idle, .loading:
            LoadingState("Recalling what Sali knows…")
        case .failed(let message):
            ErrorStateView(message) { Task { await viewModel.load(api: appState.api) } }
        case .loaded:
            List {
                summarySection
                searchSection
                if !viewModel.searchText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    searchResultsSection
                } else {
                    recentSection
                }
                if !viewModel.conflicts.isEmpty { conflictsSection }
            }
            .listStyle(.insetGrouped)
            .animation(reduceMotion ? nil : Theme.Motion.standard, value: viewModel.searchResults.count)
        }
    }

    // MARK: sections

    private var summarySection: some View {
        Section {
            HStack(spacing: Theme.Spacing.l) {
                statTile(value: viewModel.counts?.total, label: "Memories")
                statTile(value: viewModel.counts?.experiences, label: "Experiences")
                statTile(value: viewModel.counts?.openConflicts, label: "Open conflicts",
                        tint: (viewModel.counts?.openConflicts ?? 0) > 0 ? Theme.Colors.warn : nil)
            }
            .padding(.vertical, Theme.Spacing.xs)
        } header: {
            Text("What Sali remembers")
        } footer: {
            Text("Every memory here carries where it came from, how confident Sali is, and how fresh it is — nothing is treated as fact without evidence.")
        }
    }

    private func statTile(value: Int?, label: String, tint: Color? = nil) -> some View {
        VStack(spacing: Theme.Spacing.xs) {
            Text(value.map(String.init) ?? "—")
                .font(Theme.Typography.title)
                .foregroundStyle(tint ?? Theme.Colors.primaryText)
            Text(label)
                .font(Theme.Typography.caption)
                .foregroundStyle(Theme.Colors.secondaryText)
        }
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .combine)
    }

    private var searchSection: some View {
        Section {
            HStack {
                Image(systemName: "magnifyingglass").foregroundStyle(Theme.Colors.secondaryText)
                TextField("Search what Sali knows about a topic", text: $viewModel.searchText)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .onSubmit { Task { await viewModel.search(api: appState.api) } }
                if !viewModel.searchText.isEmpty {
                    Button {
                        viewModel.clearSearch()
                    } label: {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(Theme.Colors.secondaryText)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Clear search")
                }
            }
            .onChange(of: viewModel.searchText) { _, _ in
                viewModel.scheduleSearch(api: appState.api)
            }
        }
    }

    private var searchResultsSection: some View {
        Section("Relevant to \u{201c}\(viewModel.searchText)\u{201d}") {
            if viewModel.isSearching {
                LoadingState("Searching…").frame(height: 60)
            } else if let error = viewModel.searchError {
                Text(error).font(Theme.Typography.caption).foregroundStyle(Theme.Colors.warn)
            } else if viewModel.searchResults.isEmpty {
                Text("Nothing relevant to that topic yet.")
                    .font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
            } else {
                ForEach(viewModel.searchResults) { experience in
                    ExperienceRow(experience: experience)
                        .contentShape(Rectangle())
                        .onTapGesture { selectedExperience = experience }
                }
            }
        }
    }

    private var recentSection: some View {
        Section("Recent experiences") {
            if viewModel.recentExperiences.isEmpty {
                EmptyStateView(icon: "brain", title: "Nothing learned yet",
                               message: "As Sali completes tasks and verifies outcomes, the experiences that shaped its behavior will show up here.")
                    .listRowInsets(EdgeInsets())
            } else {
                ForEach(viewModel.recentExperiences) { experience in
                    ExperienceRow(experience: experience)
                        .contentShape(Rectangle())
                        .onTapGesture { selectedExperience = experience }
                }
            }
        }
    }

    private var conflictsSection: some View {
        Section {
            ForEach(viewModel.conflicts) { conflict in
                ConflictRow(conflict: conflict)
            }
        } header: {
            Text("Conflicts")
        } footer: {
            Text("When new evidence contradicts something Sali believed, it's recorded here rather than silently overwritten, and resolved by which source is more trustworthy.")
        }
    }
}

// MARK: - Rows

private struct ExperienceRow: View {
    let experience: Experience

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            Text(experience.summary ?? experience.objective ?? "An experience Sali recorded")
                .font(Theme.Typography.body.weight(.medium))
                .lineLimit(2)

            if let objective = experience.objective, experience.summary != nil {
                Label(objective, systemImage: "target")
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .lineLimit(1)
            }
            if let outcome = experience.outcome {
                Label(outcome, systemImage: "flag.checkered")
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .lineLimit(1)
            }

            HStack(spacing: Theme.Spacing.m) {
                if let confidence = experience.confidence {
                    ConfidenceBar(confidence).frame(width: 60)
                }
                if let state = experience.evidenceState {
                    StatusPill(state.replacingUnderscores, color: color(forEvidence: state))
                }
                Spacer()
                if let createdAt = experience.createdAt {
                    Text(createdAt.saliRelative)
                        .font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.secondaryText)
                }
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }

    private func color(forEvidence state: String) -> Color {
        switch state {
        case "verified", "promoted": Theme.Colors.ok
        case "contradicted", "rejected": Theme.Colors.danger
        default: Theme.Colors.info
        }
    }
}

private struct ConflictRow: View {
    let conflict: MemoryConflictItem

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack {
                Label {
                    Text("\(conflict.newSource ?? "New evidence") vs. \(conflict.oldSource ?? "prior belief")")
                        .font(Theme.Typography.body)
                        .lineLimit(2)
                } icon: {
                    Image(systemName: "exclamationmark.arrow.triangle.2.circlepath")
                        .foregroundStyle(conflict.isOpen ? Theme.Colors.warn : Theme.Colors.secondaryText)
                }
                Spacer()
                StatusPill(conflict.status ?? "open", color: conflict.isOpen ? Theme.Colors.warn : Theme.Colors.ok)
            }
            if let resolution = conflict.resolution, !resolution.isEmpty {
                Text(resolution)
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }
            if let createdAt = conflict.createdAt {
                Text(createdAt.saliRelative)
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Detail sheet

private struct ExperienceDetailSheet: View {
    let experience: Experience
    @ObservedObject var viewModel: MemoryViewModel
    let api: APIClient
    let canManage: Bool

    @Environment(\.dismiss) private var dismiss
    @State private var showForgetSheet = false

    var body: some View {
        NavigationStack {
            List {
                Section("What happened") {
                    LabeledContent("Summary") {
                        Text(experience.summary ?? "Not recorded")
                    }
                    if let objective = experience.objective {
                        LabeledContent("Objective") { Text(objective) }
                    }
                    if let outcome = experience.outcome {
                        LabeledContent("Outcome") { Text(outcome) }
                    }
                    if let procedure = experience.procedure {
                        LabeledContent("How") { Text(procedure) }
                    }
                }

                Section("Confidence & freshness") {
                    if let confidence = experience.confidence {
                        HStack {
                            Text("Confidence")
                            Spacer()
                            ConfidenceBar(confidence).frame(width: 100)
                            Text("\(Int((confidence * 100).rounded()))%")
                                .font(Theme.Typography.caption)
                                .foregroundStyle(Theme.Colors.secondaryText)
                        }
                    }
                    if let state = experience.evidenceState {
                        LabeledContent("Evidence state") { Text(state.replacingUnderscores) }
                    }
                    if let createdAt = experience.createdAt {
                        LabeledContent("Learned") { Text(createdAt.saliRelative) }
                    } else {
                        LabeledContent("Learned") { Text("Timestamp not recorded").foregroundStyle(Theme.Colors.secondaryText) }
                    }
                }

                Section("Provenance") {
                    provenanceContent
                }

                if canManage {
                    Section {
                        Button(role: .destructive) { showForgetSheet = true } label: {
                            Label("Forget this experience…", systemImage: "trash")
                        }
                    } footer: {
                        Text("Retiring a memory is a deliberate, confirmed action — owner or controller only.")
                    }
                }
            }
            .navigationTitle("Experience")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
            }
            .task { await viewModel.loadProvenance(memoryId: experience.experienceId ?? "", api: api) }
            .sheet(isPresented: $showForgetSheet) { ForgetExperienceSheet() }
        }
    }

    @ViewBuilder private var provenanceContent: some View {
        switch viewModel.provenance {
        case .idle, .loading:
            HStack { ProgressView(); Text("Tracing why Sali believes this…").font(Theme.Typography.caption) }
        case .failed(let message):
            Text(message).font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
        case .loaded:
            if let detail = viewModel.provenanceDetail {
                if let source = detail.source {
                    LabeledContent("Source") { Text(source) }
                }
                if let layer = detail.layer {
                    LabeledContent("Layer") { Text(layer) }
                }
                if let verification = detail.verification {
                    LabeledContent("Verification") { Text(verification) }
                }
                if let count = detail.evidenceCount {
                    LabeledContent("Evidence records") { Text("\(count)") }
                }
                ForEach(detail.evidence ?? []) { item in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.source ?? "evidence").font(Theme.Typography.caption.weight(.medium))
                        if let note = item.note, !note.isEmpty {
                            Text(note).font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
                        }
                    }
                }
            } else {
                Text("No further provenance recorded.")
                    .font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
            }
        }
    }
}

/// Deliberately NOT wired to a backend call: there is no delete/retire endpoint yet. This sheet is the
/// natural-confirmation surface the feature will use once one exists — presented honestly as "coming",
/// never faking an action that wouldn't actually do anything.
private struct ForgetExperienceSheet: View {
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: Theme.Spacing.l) {
                Image(systemName: "hourglass")
                    .font(.system(size: 32))
                    .foregroundStyle(Theme.Colors.secondaryText)
                Text("Retiring a memory is permanent and can change how Sali behaves in similar situations later. Because that's consequential, it will require a deliberate, explicit confirmation here — the same as this screen — before anything is removed.")
                    .font(Theme.Typography.body)
                    .foregroundStyle(Theme.Colors.secondaryText)
                Text("This action isn't available yet — Sali doesn't have a way to retire a memory from the server side. Coming soon.")
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.warn)
                Spacer(minLength: 0)
                Button("Close") { dismiss() }
                    .buttonStyle(.borderedProminent)
                    .frame(maxWidth: .infinity)
            }
            .padding(Theme.Spacing.xl)
            .navigationTitle("Forget this experience")
            .navigationBarTitleDisplayMode(.inline)
        }
        .presentationDetents([.medium])
    }
}

private extension String {
    var replacingUnderscores: String { replacingOccurrences(of: "_", with: " ") }
}
