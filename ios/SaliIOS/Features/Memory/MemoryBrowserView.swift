import SwiftUI

/// Browse and manage what Sali believes.
///
/// The overview screen answers "how much does Sali remember and what shaped it". This one answers the
/// harder question — "what exactly does it believe, and is any of it wrong" — and lets Almir do something
/// about it. It is the client for the canonical memory API and holds NO store of its own: every list, every
/// edit and every deletion is the host's `sali.memory` table, read and written through
/// `/api/v1/memory/*`. There is one memory system and this is a window onto it.
///
/// The four verbs are deliberately not the same weight, because they are not the same act:
///   REVISE      — the belief is wrong; write the correct one. The old row is retired and linked, so the
///                 correction has a history rather than overwriting one.
///   INVALIDATE  — the belief is no longer current. Reversible; the row stays and can be restored.
///   RESTORE     — undo an invalidation. Refused by the host for a superseded row, because putting the
///                 loser of a correction back beside the winner makes one claim answer two ways.
///   DELETE      — erase it. Irreversible, owner-only, and for a privacy request rather than a mistake.
/// Only the last is destructive, and only the last demands a typed reason plus an explicit confirmation.

// MARK: - Wire shapes (`GET /api/v1/memory/list`, `/memory/stats`)

/// One memory as the API returns it. Every key is always present and may be null, so optionals here mean
/// "the host said null", never "the field was missing" — decoding stays honest about which it was.
struct MemoryRecord: Decodable, Identifiable, Equatable {
    let id: String
    let layer: String
    let content: String
    let source: String
    let scope: String?
    let kind: String?
    let confidence: Double
    let importance: Double
    let reliability: Double
    let evidenceCount: Int
    let accessCount: Int
    let functional: Bool
    let claimKey: String?
    let needsGrounding: Bool
    let freshness: String?
    let embedStatus: String?
    let isCurrent: Bool
    let supersededBy: String?
    let forgetReason: String?
    let validFrom: Date?
    let validUntil: Date?
    let lastVerified: Date?
    let lastAccessed: Date?
    let createdAt: Date?

    enum CodingKeys: String, CodingKey {
        case id, layer, content, source, scope, kind, confidence, importance, reliability, functional
        case evidenceCount = "evidence_count", accessCount = "access_count"
        case claimKey = "claim_key", needsGrounding = "needs_grounding", freshness
        case embedStatus = "embed_status", isCurrent = "is_current", supersededBy = "superseded_by"
        case forgetReason = "forget_reason", validFrom = "valid_from", validUntil = "valid_until"
        case lastVerified = "last_verified", lastAccessed = "last_accessed", createdAt = "created_at"
    }

    /// What this row IS, in one word, for the row's leading label.
    var layerLabel: String {
        switch layer {
        case "system_env": "environment"
        case "semantic": "fact"
        default: layer
        }
    }

    /// Where it came from, said the way a person would.
    var sourceLabel: String {
        switch source {
        case "user_explicit": "you told me"
        case "conversation": "came up in conversation"
        case "system_observation": "I looked"
        case "file_observation": "from a file"
        case "tool_result": "from a tool"
        case "external_source": "from the web"
        case "inference": "I worked it out"
        case "procedure_execution": "from doing it"
        default: source
        }
    }
}

private struct MemoryListResponse: Decodable {
    let memories: [MemoryRecord]
    let hasMore: Bool
    let nextCursor: Cursor?

    struct Cursor: Decodable, Equatable {
        let createdAt: String
        let id: String
        enum CodingKeys: String, CodingKey { case createdAt = "created_at", id }
    }
    enum CodingKeys: String, CodingKey {
        case memories, hasMore = "has_more", nextCursor = "next_cursor"
    }
}

struct MemoryStats: Decodable, Equatable {
    let totals: Totals
    let byLayer: [Bucket]
    let bySource: [Bucket]

    struct Totals: Decodable, Equatable {
        let current: Int
        let invalidated: Int
        let unverified: Int
        let superseded: Int
        let unembedded: Int
        let total: Int
    }
    struct Bucket: Decodable, Equatable, Identifiable {
        let name: String
        let n: Int
        var id: String { name }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: Key.self)
            // The host names the key after the dimension (`layer` or `source`); take whichever is there
            // rather than requiring two nearly-identical structs.
            name = (try? c.decode(String.self, forKey: .layer))
                ?? (try? c.decode(String.self, forKey: .source)) ?? "?"
            n = (try? c.decode(Int.self, forKey: .n)) ?? 0
        }
        enum Key: String, CodingKey { case layer, source, n }
    }
    enum CodingKeys: String, CodingKey { case totals, byLayer = "by_layer", bySource = "by_source" }
}

private struct ReviseResponse: Decodable { let revised: Bool }
private struct RestoreResponse: Decodable { let restored: Bool }
private struct InvalidateResponse: Decodable { let invalidated: Bool }

// MARK: - View model

@MainActor
final class MemoryBrowserViewModel: ObservableObject {
    @Published private(set) var rows: [MemoryRecord] = []
    @Published private(set) var stats: MemoryStats?
    @Published private(set) var loading = false
    @Published private(set) var loadingMore = false
    @Published private(set) var hasMore = false
    @Published var error: String?
    @Published var confirmation: String?

    @Published var query = ""
    @Published var layer: String?
    @Published var source: String?
    @Published var state: String = "current"
    @Published var onlyUnverified = false

    private var cursor: MemoryListResponse.Cursor?

    /// Any memory write on the host should be visible here without a pull-to-refresh.
    static let liveTriggers: Set<String> = [
        "memory.created", "memory.forgotten", "memory.corroborated", "memory.verified",
        "memory.updated", "memory.restored", "memory.deleted", "memory.consolidated",
    ]

    private func path(cursor c: MemoryListResponse.Cursor?) -> (String, [String: String]) {
        var q: [String: String] = ["state": state, "limit": "40"]
        if !query.trimmingCharacters(in: .whitespaces).isEmpty { q["q"] = query }
        if let layer { q["layer"] = layer }
        if let source { q["source"] = source }
        if onlyUnverified { q["needs_grounding"] = "true" }
        if let c {
            q["cursor_created_at"] = c.createdAt
            q["cursor_id"] = c.id
        }
        return ("memory/list", q)
    }

    func load(api: APIClient) async {
        loading = rows.isEmpty
        defer { loading = false }
        error = nil
        cursor = nil
        let (p, q) = path(cursor: nil)
        do {
            async let listing: MemoryListResponse = api.get(p, query: q)
            async let counts: MemoryStats = api.get("memory/stats")
            let (list, s) = try await (listing, counts)
            rows = list.memories
            hasMore = list.hasMore
            cursor = list.nextCursor
            stats = s
        } catch {
            // Keep whatever is on screen: a failed refresh must not blank a list Almir is reading.
            if rows.isEmpty { self.error = (error as? APIError)?.errorDescription ?? "Couldn't read memory." }
        }
    }

    /// Keyset paging — the host returns the cursor, we hand it straight back. No offsets, so page 900
    /// costs the same as page 1.
    func loadMore(api: APIClient) async {
        guard hasMore, !loadingMore, let c = cursor else { return }
        loadingMore = true
        defer { loadingMore = false }
        let (p, q) = path(cursor: c)
        if let list: MemoryListResponse = try? await api.get(p, query: q) {
            rows.append(contentsOf: list.memories)
            hasMore = list.hasMore
            cursor = list.nextCursor
        }
    }

    private func esc(_ s: String) -> String {
        s.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? s
    }

    func revise(_ memory: MemoryRecord, to content: String, api: APIClient) async {
        await mutate(api: api, confirm: "Corrected.") {
            let _: ReviseResponse = try await api.post(
                "memory/\(memory.id)/revise?content=\(self.esc(content))&reason=\(self.esc("corrected on iPhone"))")
        }
    }

    func invalidate(_ memory: MemoryRecord, reason: String, api: APIClient) async {
        await mutate(api: api, confirm: "Retired — you can restore it.") {
            let _: InvalidateResponse = try await api.post(
                "memory/\(memory.id)/invalidate?reason=\(self.esc(reason))")
        }
    }

    func restore(_ memory: MemoryRecord, api: APIClient) async {
        await mutate(api: api, confirm: "Back in current knowledge.") {
            let _: RestoreResponse = try await api.post("memory/\(memory.id)/restore")
        }
    }

    func delete(_ memory: MemoryRecord, reason: String, api: APIClient) async {
        await mutate(api: api, confirm: "Erased.") {
            try await api.deleteVoid("memory/\(memory.id)?confirm=true&reason=\(self.esc(reason))")
        }
    }

    /// One shape for every write: run it, say what happened in plain words, and re-read from the host so
    /// the screen shows what is actually stored rather than what we hoped we stored.
    private func mutate(api: APIClient, confirm: String, _ work: () async throws -> Void) async {
        error = nil
        do {
            try await work()
            confirmation = confirm
            await load(api: api)
        } catch {
            self.error = (error as? APIError)?.errorDescription ?? "That didn't work."
        }
    }
}

// MARK: - Browser

struct MemoryBrowserView: View {
    @EnvironmentObject var appState: AppState
    @StateObject private var viewModel = MemoryBrowserViewModel()
    @State private var selected: MemoryRecord?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private static let layers = ["preference", "semantic", "episodic", "procedural",
                                 "identity", "system_env"]

    var body: some View {
        List {
            if let stats = viewModel.stats { statsSection(stats) }
            filtersSection
            resultsSection
        }
        .listStyle(.insetGrouped)
        .saliList()
        .navigationTitle("Browse memory")
        .navigationBarTitleDisplayMode(.inline)
        .searchable(text: $viewModel.query, prompt: "Search what Sali remembers")
        .onSubmit(of: .search) { Task { await viewModel.load(api: appState.api) } }
        .task { if viewModel.rows.isEmpty { await viewModel.load(api: appState.api) } }
        .refreshable { await viewModel.load(api: appState.api) }
        .onChange(of: appState.latestEvent?.id) { _, _ in
            guard let raw = appState.latestEvent?.rawType,
                  MemoryBrowserViewModel.liveTriggers.contains(raw) else { return }
            Task { await viewModel.load(api: appState.api) }
        }
        .sheet(item: $selected) { record in
            MemoryDetailSheet(record: record, viewModel: viewModel, api: appState.api,
                              canControl: appState.role.canControl)
        }
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick), value: viewModel.rows.count)
    }

    private func statsSection(_ stats: MemoryStats) -> some View {
        Section {
            HStack(alignment: .top, spacing: Theme.Spacing.xl) {
                stat(stats.totals.current, "Current")
                stat(stats.totals.invalidated, "Retired")
                if stats.totals.unverified > 0 {
                    stat(stats.totals.unverified, "Unverified", tint: Theme.Colors.warn)
                }
                Spacer(minLength: 0)
            }
            .padding(.vertical, Theme.Spacing.xs)
            if !stats.byLayer.isEmpty {
                Text(stats.byLayer.map { "\($0.name.replacingOccurrences(of: "_", with: " ")) \($0.n)" }
                        .joined(separator: "  ·  "))
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
            }
        } header: {
            SectionHeader("What Sali holds", subtitle: "Counts from the host, not from this phone.")
        }
    }

    private func stat(_ n: Int, _ label: String, tint: Color? = nil) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("\(n)")
                .font(Theme.Typography.numeral)
                .foregroundStyle(tint ?? Theme.Colors.primaryText)
            Text(label)
                .font(Theme.Typography.metadata)
                .foregroundStyle(Theme.Colors.tertiaryText)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(n) \(label)")
    }

    private var filtersSection: some View {
        Section {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: Theme.Spacing.s) {
                    chip("All", active: viewModel.layer == nil) { viewModel.layer = nil }
                    ForEach(Self.layers, id: \.self) { l in
                        chip(l.replacingOccurrences(of: "_", with: " "), active: viewModel.layer == l) {
                            viewModel.layer = (viewModel.layer == l) ? nil : l
                        }
                    }
                }
                .padding(.vertical, 2)
            }
            .listRowInsets(EdgeInsets(top: Theme.Spacing.s, leading: Theme.Spacing.listMargin,
                                      bottom: Theme.Spacing.s, trailing: Theme.Spacing.listMargin))
            HStack(spacing: Theme.Spacing.s) {
                chip("Current", active: viewModel.state == "current") { viewModel.state = "current" }
                chip("Retired", active: viewModel.state == "invalid") { viewModel.state = "invalid" }
                chip("Everything", active: viewModel.state == "all") { viewModel.state = "all" }
                Spacer(minLength: 0)
                chip("Unverified", active: viewModel.onlyUnverified) {
                    viewModel.onlyUnverified.toggle()
                }
            }
        } header: {
            SectionHeader("Filter", emphasis: .secondary)
        }
        .onChange(of: viewModel.layer) { _, _ in Task { await viewModel.load(api: appState.api) } }
        .onChange(of: viewModel.state) { _, _ in Task { await viewModel.load(api: appState.api) } }
        .onChange(of: viewModel.onlyUnverified) { _, _ in Task { await viewModel.load(api: appState.api) } }
    }

    private func chip(_ label: String, active: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label)
                .font(Theme.Typography.footnote.weight(active ? .semibold : .regular))
                .foregroundStyle(active ? Theme.Colors.onAccent : Theme.Colors.secondaryText)
                .padding(.horizontal, Theme.Spacing.m)
                .frame(minHeight: 32)
                .background(active ? Theme.Colors.accent : Theme.Colors.surfaceSunken)
                .clipShape(Capsule(style: .continuous))
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(active ? [.isSelected] : [])
    }

    @ViewBuilder private var resultsSection: some View {
        Section {
            if viewModel.loading {
                SaliSkeletonRow(lines: 2, showsPill: true)
            } else if let error = viewModel.error {
                ErrorStateView(error) { Task { await viewModel.load(api: appState.api) } }
            } else if viewModel.rows.isEmpty {
                Text(viewModel.query.isEmpty
                     ? "Nothing here yet with those filters."
                     : "Nothing matching “\(viewModel.query)”.")
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
            } else {
                ForEach(viewModel.rows) { record in
                    Button { selected = record } label: { MemoryRow(record: record) }
                        .buttonStyle(.plain)
                }
                if viewModel.hasMore {
                    Button {
                        Task { await viewModel.loadMore(api: appState.api) }
                    } label: {
                        HStack {
                            Spacer()
                            if viewModel.loadingMore {
                                ProgressView()
                            } else {
                                Text("Load more").font(Theme.Typography.footnote.weight(.medium))
                            }
                            Spacer()
                        }
                        .frame(minHeight: 44)
                    }
                    .tint(Theme.Colors.primaryText)
                }
            }
        } header: {
            SectionHeader(viewModel.state == "invalid" ? "Retired" : "Memories", emphasis: .secondary)
        } footer: {
            if let confirmation = viewModel.confirmation {
                SectionFooter(confirmation)
            }
        }
    }
}

// MARK: - Row

private struct MemoryRow: View {
    let record: MemoryRecord

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            Text(record.content)
                .font(Theme.Typography.body)
                .foregroundStyle(record.isCurrent ? Theme.Colors.primaryText : Theme.Colors.secondaryText)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: Theme.Spacing.xs) {
                MicroLabel(record.layerLabel, ink: Theme.Colors.tertiaryText)
                Text("·").foregroundStyle(Theme.Colors.tertiaryText)
                Text(record.sourceLabel)
                if record.needsGrounding {
                    Text("·").foregroundStyle(Theme.Colors.tertiaryText)
                    Text("unverified").foregroundStyle(Theme.Colors.warn)
                }
                if !record.isCurrent {
                    Text("·").foregroundStyle(Theme.Colors.tertiaryText)
                    Text(record.supersededBy != nil ? "replaced" : "retired")
                }
            }
            .font(Theme.Typography.metadata)
            .foregroundStyle(Theme.Colors.tertiaryText)
            .lineLimit(1)
        }
        .padding(.vertical, Theme.Spacing.xs)
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Detail + actions

private struct MemoryDetailSheet: View {
    let record: MemoryRecord
    @ObservedObject var viewModel: MemoryBrowserViewModel
    let api: APIClient
    let canControl: Bool

    @Environment(\.dismiss) private var dismiss
    @State private var editing = false
    @State private var draft = ""
    @State private var confirmingDelete = false
    @State private var deleteReason = ""

    var body: some View {
        NavigationStack {
            List {
                Section {
                    if editing {
                        TextEditor(text: $draft)
                            .font(Theme.Typography.body)
                            .frame(minHeight: 120)
                        SectionFooter("Saving writes a new memory and retires this one, linked to it — "
                                      + "so the correction has a history instead of overwriting one.")
                    } else {
                        Text(record.content)
                            .font(Theme.Typography.body)
                            .foregroundStyle(Theme.Colors.primaryText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                } header: {
                    SectionHeader(record.isCurrent ? "What Sali believes" : "What Sali believed")
                }

                Section {
                    MemoryFactRow("Kind") { Text(record.layerLabel) }
                    MemoryFactRow("Where from") { Text(record.sourceLabel) }
                    MemoryFactRow("Confidence") { MemoryConfidence(value: record.confidence) }
                    MemoryFactRow("Importance") { Text(String(format: "%.2f", record.importance)) }
                    MemoryFactRow("Evidence") {
                        Text("\(record.evidenceCount) " + (record.evidenceCount == 1 ? "observation" : "observations"))
                    }
                    MemoryFactRow("Recalled") {
                        Text(record.accessCount == 0 ? "never used yet" : "\(record.accessCount)×")
                    }
                    if record.needsGrounding {
                        MemoryFactRow("Status") {
                            Text("unverified — Sali hasn't checked this")
                                .foregroundStyle(Theme.Colors.warn)
                        }
                    }
                    if let claim = record.claimKey {
                        MemoryFactRow("Claim") { Text(claim).font(Theme.Typography.monoSmall) }
                    }
                } header: {
                    SectionHeader("Provenance", emphasis: .secondary)
                } footer: {
                    if record.functional {
                        SectionFooter("Single-valued: saying something new about this topic replaces it "
                                      + "rather than sitting beside it.")
                    }
                }

                Section {
                    if let created = record.createdAt {
                        MemoryFactRow("First learned") { Text(created.saliRelative) }
                    }
                    if let verified = record.lastVerified {
                        MemoryFactRow("Last checked") { Text(verified.saliRelative) }
                    }
                    if let until = record.validUntil {
                        MemoryFactRow("Retired") { Text(until.saliRelative) }
                    }
                    if let reason = record.forgetReason, !reason.isEmpty {
                        MemoryFactRow("Because") { Text(reason) }
                    }
                } header: {
                    SectionHeader("When", emphasis: .secondary)
                }

                if canControl { actionsSection }
            }
            .listStyle(.insetGrouped)
            .saliList()
            .navigationTitle("Memory")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button(editing ? "Save" : "Done") {
                        if editing {
                            let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
                            guard !text.isEmpty, text != record.content else { editing = false; return }
                            Task {
                                await viewModel.revise(record, to: text, api: api)
                                dismiss()
                            }
                        } else {
                            dismiss()
                        }
                    }
                }
            }
            .sheet(isPresented: $confirmingDelete) {
                NaturalConfirmationSheet(
                    title: "Erase this memory?",
                    explanation: "This removes the belief itself and everything that points at it — the "
                        + "search index, any graph links, any contradiction records. It cannot be undone.\n\n"
                        + "It does NOT erase your conversation: what was actually said stays in the "
                        + "transcript. If the memory is simply out of date, retire it instead — that keeps "
                        + "the history and you can restore it.",
                    confirmLabel: "Erase permanently",
                    confirmDisabled: deleteReason.trimmingCharacters(in: .whitespaces).isEmpty
                ) {
                    Task {
                        await viewModel.delete(record, reason: deleteReason, api: api)
                        dismiss()
                    }
                }
            }
        }
    }

    private var actionsSection: some View {
        Section {
            if record.isCurrent {
                Button(editing ? "Cancel edit" : "Correct this") {
                    draft = record.content
                    editing.toggle()
                }
                .tint(Theme.Colors.primaryText)
                Button("Retire it") {
                    Task {
                        await viewModel.invalidate(record, reason: "retired from iPhone", api: api)
                        dismiss()
                    }
                }
                .tint(Theme.Colors.primaryText)
            } else if record.supersededBy == nil {
                Button("Restore it") {
                    Task {
                        await viewModel.restore(record, api: api)
                        dismiss()
                    }
                }
                .tint(Theme.Colors.primaryText)
            } else {
                Text("Replaced by a newer version — restore the correction instead of this one.")
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }
            Button("Erase permanently…", role: .destructive) {
                // A reason is required by the host and is kept in the append-only event log, so it is
                // filled with something truthful rather than left blank for the sheet to block on.
                deleteReason = "erased from iPhone"
                confirmingDelete = true
            }
        } header: {
            SectionHeader("Actions", emphasis: .secondary)
        } footer: {
            SectionFooter("Retiring is reversible and keeps the history. Erasing is not, and is meant for "
                          + "something that should never have been written down.")
        }
    }
}


// MARK: - Local primitives
//
// `DetailRow` and `ConfidenceReadout` already exist three times over — file-private in MemoryView,
// LearningView and SystemView. Promoting one to shared made the other two ambiguous, so the browser gets
// its own rather than forcing a refactor of two unrelated screens in the middle of a memory change.

private struct MemoryFactRow<Value: View>: View {
    let label: String
    @ViewBuilder var value: Value

    init(_ label: String, @ViewBuilder value: () -> Value) {
        self.label = label
        self.value = value()
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.m) {
            Text(label)
                .font(Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.secondaryText)
            Spacer(minLength: Theme.Spacing.s)
            value
                .font(Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.primaryText)
                .multilineTextAlignment(.trailing)
        }
        .frame(minHeight: 32)
        .accessibilityElement(children: .combine)
    }
}

/// Confidence as a number AND a bar, because 0.77 means nothing on its own and a bar alone cannot be read
/// aloud. Monochrome — confidence is not a warning.
private struct MemoryConfidence: View {
    let value: Double

    var body: some View {
        HStack(spacing: Theme.Spacing.s) {
            Text(String(format: "%.2f", value))
                .font(Theme.Typography.numeralSmall)
            Capsule()
                .fill(Theme.Colors.placeholder)
                .frame(width: 44, height: 4)
                .overlay(alignment: .leading) {
                    Capsule()
                        .fill(Theme.Colors.accent)
                        .frame(width: 44 * max(0, min(1, value)), height: 4)
                }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Confidence \(String(format: "%.0f", value * 100)) percent")
    }
}
