import Foundation
import SwiftUI

// Memory — what Sali remembers (§16/§40). This is deliberately NOT a raw database browser: it surfaces
// distilled *experiences* (the durable, evidence-backed record of what Sali did and learned), the counts
// behind them, and any open contradictions — every one shown with its provenance, confidence, and
// freshness rather than presented as flat fact (golden rule, CLAUDE.md).
//
// The screen reads top to bottom as one sentence: what is in there → ask it something → what came back →
// what disagrees → and only then, add to it. Teaching sits last because writing to Sali's memory is the
// consequential act on this screen, not the first thing a reader should meet.

// MARK: - Forgiving decode

/// One bad element must degrade itself, never the array around it. A single unreadable row — a date in
/// an unexpected shape, a field that arrived as a list where a string was assumed — has blanked whole
/// screens in this app before; wrapping each element keeps that failure local to the row.
private struct Failable<Wrapped: Decodable>: Decodable {
    let value: Wrapped?
    init(from decoder: Decoder) throws { value = try? Wrapped(from: decoder) }
}

/// Spelled out because a generic type gets no implicit `Sendable` — and every response below stores
/// `[Failable<…>]`, so without this they cannot cross back from the `APIClient` actor.
extension Failable: Sendable where Wrapped: Sendable {}

private extension Array {
    func compacted<T>() -> [T] where Element == Failable<T> { compactMap(\.value) }
}

/// A timestamp that degrades to "unknown" rather than throwing. `APIClient.decoder` understands only
/// RFC-3339 with an offset; a timezone-naive value, a space-separated one, a two-digit Postgres offset
/// or a bare epoch are understood here, and anything left over becomes `nil` — one unreadable date
/// costs its own field and nothing more.
private struct WireDate: Decodable {
    let value: Date?

    init(from decoder: Decoder) throws {
        guard let container = try? decoder.singleValueContainer(), !container.decodeNil() else {
            value = nil
            return
        }
        if let text = try? container.decode(String.self) { value = Self.parse(text); return }
        if let seconds = try? container.decode(Double.self) {
            value = Date(timeIntervalSince1970: seconds)
            return
        }
        value = nil
    }

    private static let isoOptions: [ISO8601DateFormatter.Options] = [
        [.withInternetDateTime, .withFractionalSeconds], [.withInternetDateTime],
    ]

    private static let plainFormats = [
        "yyyy-MM-dd'T'HH:mm:ss.SSSSSS", "yyyy-MM-dd'T'HH:mm:ss.SSS", "yyyy-MM-dd'T'HH:mm:ss",
        "yyyy-MM-dd HH:mm:ss.SSSSSS", "yyyy-MM-dd HH:mm:ss.SSS", "yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd",
    ]

    static func parse(_ raw: String) -> Date? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        var candidate = trimmed
        if let last = candidate.last, last.isNumber, candidate.count > 3 {
            let tail = candidate.suffix(3)
            if (tail.first == "+" || tail.first == "-"), tail.dropFirst().allSatisfy(\.isNumber) {
                candidate += ":00"
            }
        }
        for spelling in [candidate, candidate.replacingOccurrences(of: " ", with: "T")] {
            for options in isoOptions {
                let formatter = ISO8601DateFormatter()
                formatter.formatOptions = options
                if let date = formatter.date(from: spelling) { return date }
            }
        }
        for format in plainFormats {
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "en_US_POSIX")
            formatter.timeZone = TimeZone(secondsFromGMT: 0)
            formatter.dateFormat = format
            if let date = formatter.date(from: trimmed) { return date }
        }
        return nil
    }
}

/// A field the host may send as one string or as a list of them. `structured['procedure']` is a LIST of
/// tool names (`experience.py: _build_record`), which decoded as a `String` throws and took the entire
/// search response with it — every search on this screen failed the moment a matched experience had a
/// recorded procedure. Read both shapes; refuse neither.
private struct WireStrings: Decodable {
    let values: [String]

    init(from decoder: Decoder) throws {
        if let container = try? decoder.singleValueContainer() {
            if container.decodeNil() { values = []; return }
            if let list = try? container.decode([String].self) { values = list; return }
            if let single = try? container.decode(String.self) {
                values = single.isEmpty ? [] : [single]
                return
            }
        }
        values = []
    }
}

// MARK: - Wire shapes (`src/sali/api/routes/api.py`)

/// `GET /api/v1/memory` → `{ counts: {...}, recent_experiences: [...] }`.
private struct MemoryOverviewResponse: Decodable {
    let counts: MemoryCounts?
    let recentExperiences: [Failable<MemoryExperience>]?
    var experiences: [MemoryExperience] { (recentExperiences ?? []).compacted() }
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

    /// Biggest layer first, so the composition reads as a ranking rather than as a dictionary dump.
    var layers: [MemoryLayerCount] {
        (byLayer ?? [:])
            .filter { $0.value > 0 }
            .sorted { $0.value == $1.value ? $0.key < $1.key : $0.value > $1.value }
            .map { MemoryLayerCount(name: $0.key, count: $0.value) }
    }
}

private struct MemoryLayerCount: Identifiable {
    let name: String
    let count: Int
    var id: String { name }
}

private struct ExperiencesResponse: Decodable {
    let experiences: [Failable<MemoryExperience>]?
    var items: [MemoryExperience] { (experiences ?? []).compacted() }
}

/// One distilled experience. `recent()` sends `id/content/evidence_state/objective`;
/// `relevant_experiences()` adds `procedure`, `failures` and `scope`. Everything else is decoded only
/// because a richer shape may arrive later — nothing here is invented to fill a gap.
private struct MemoryExperience: Decodable, Identifiable {
    let memoryId: String?
    let content: String?
    let summary: String?
    let objective: String?
    let outcome: String?
    let scope: String?
    let evidenceState: String?
    let confidence: Double?
    let procedure: WireStrings?
    let failures: [Failable<ExperienceFailure>]?
    let created: WireDate?

    /// Stable across renders — a computed `UUID()` fallback re-identifies the row every time SwiftUI
    /// looks at it, which tears `ForEach` diffing (and every animation and tap on it) apart.
    var id: String { memoryId ?? "experience|\(content ?? summary ?? "")|\(objective ?? "")" }
    var steps: [String] { procedure?.values ?? [] }
    var whatFailed: [ExperienceFailure] { (failures ?? []).compacted() }
    var createdAt: Date? { created?.value }

    /// The host stores an experience as one blob — `Experience — {objective}: {summary}` — and for some
    /// rows that blob is a whole step prompt with embedded newlines. Left as-is it renders as a wall of
    /// text with the objective said twice. Only the host's own prefix is dropped here and runs of
    /// whitespace collapsed; not one word is invented or reordered.
    private var distilled: String {
        var text = firstNonEmpty(summary, content) ?? ""
        for prefix in ["Experience — ", "Experience - ", "Experience: "] where text.hasPrefix(prefix) {
            text.removeFirst(prefix.count)
            break
        }
        if let objective, !objective.isEmpty, text.hasPrefix(objective) {
            text.removeFirst(objective.count)
            while let first = text.first, first == ":" || first == " " { text.removeFirst() }
        }
        return text
    }

    /// What Sali was trying to do — the objective when the host recorded one, otherwise the first line
    /// of what it did record.
    var title: String {
        if let objective, !objective.isEmpty { return objective.collapsedWhitespace }
        let first = distilled.split(separator: "\n", maxSplits: 1).first.map(String.init) ?? ""
        let collapsed = first.collapsedWhitespace
        return collapsed.isEmpty ? "An experience Sali recorded" : collapsed
    }

    /// How it went — everything the record holds beyond the title, on one readable line.
    var detailText: String? {
        let remainder: String
        if objective?.isEmpty == false {
            remainder = distilled
        } else {
            let parts = distilled.split(separator: "\n", maxSplits: 1)
            remainder = parts.count > 1 ? String(parts[1]) : ""
        }
        let collapsed = remainder.collapsedWhitespace
        return collapsed.isEmpty || collapsed == title ? nil : collapsed
    }

    /// `relevant_experiences()` sends the memory row's `scope`, which on this host is the workspace
    /// path — not the "task"/"project" word the name suggests. Say whichever it actually is.
    var scopeMetadata: String? {
        guard let scope, !scope.isEmpty else { return nil }
        guard scope.contains("/") else { return "\(scope) scope" }
        let name = (scope as NSString).lastPathComponent
        return name.isEmpty ? nil : "in \(name)"
    }

    enum CodingKeys: String, CodingKey {
        case memoryId = "id", content, summary, objective, outcome, scope
        case evidenceState = "evidence_state", confidence, procedure, failures, created = "created_at"
    }
}

private struct ExperienceFailure: Decodable, Identifiable {
    let tool: String?
    let step: Int?
    let error: String?
    var id: String { "\(tool ?? "")|\(step.map(String.init) ?? "")|\(error ?? "")" }
}

// MARK: - Recall & teaching — the phone's half of `sali recall` / `sali remember`

/// `GET /api/v1/recall?q=…` → exactly what Sali would pull out of memory for that question: the same
/// retrieval it runs mid-conversation, not a second search index over the same rows. Results are held
/// only for the question currently on screen and dropped the moment it changes — this screen is a window
/// onto the host's memory, never a competing copy of it. A blank `q` is a 400, so one is never sent.
private struct RecallResponse: Decodable {
    let intent: String?
    let memories: [Failable<RecalledMemory>]?
    let graph: [Failable<GraphFact>]?
    var recalled: [RecalledMemory] { (memories ?? []).compacted() }
    var facts: [GraphFact] { (graph ?? []).compacted() }
}

private struct RecalledMemory: Decodable, Identifiable {
    let content: String?
    let confidence: Double?
    let stale: Bool?
    let layer: String?
    var id: String { "\(layer ?? "")|\(content ?? "")" }
}

/// One edge of the knowledge graph, rendered as `src —rel→ dst`.
private struct GraphFact: Decodable, Identifiable {
    let src: String?
    let rel: String?
    let dst: String?
    var id: String { "\(src ?? "")|\(rel ?? "")|\(dst ?? "")" }
}

/// `POST /api/v1/memory` → `{ id, layer, confidence }`. The layer and confidence that come back are the
/// HOST's answer for what it actually stored — nothing here guesses either.
private struct TaughtMemory: Decodable {
    let id: String?
    let layer: String?
    let confidence: Double?
}

/// The host's `MemoryLayer` also has `working`, `procedural` and `system_env`, but those are the
/// runtime's own working surfaces — a person teaching Sali a fact has no business writing into them, so
/// the picker offers only the four that describe a human-held belief. Descriptions are for the person
/// choosing; they are never sent.
private enum MemoryLayer: String, CaseIterable, Identifiable {
    case semantic, preference, episodic, identity
    var id: String { rawValue }
    var title: String { rawValue.capitalized }
    var explanation: String {
        switch self {
        case .semantic:   "A fact about the world Sali should know."
        case .preference: "How you like things done."
        case .episodic:   "Something that happened, at a point in time."
        case .identity:   "Something about who you are."
        }
    }
}

/// `GET /api/v1/memory/conflicts` → rows from the `contradiction` table
/// (`src/sali/learning/experience.py: conflicts()`). Never silently overwritten; shown for review.
private struct MemoryConflictsResponse: Decodable {
    let conflicts: [Failable<MemoryConflictItem>]?
    var items: [MemoryConflictItem] { (conflicts ?? []).compacted() }
}

private struct MemoryConflictItem: Decodable, Identifiable {
    let conflictId: String?
    let oldSource: String?
    let newSource: String?
    let resolution: String?
    let status: String?
    let created: WireDate?

    var id: String { conflictId ?? "conflict|\(oldSource ?? "")|\(newSource ?? "")" }
    var createdAt: Date? { created?.value }
    var isOpen: Bool { (status ?? "open") == "open" }

    enum CodingKeys: String, CodingKey {
        case conflictId = "id", oldSource = "old_source", newSource = "new_source"
        case resolution, status, created = "created_at"
    }
}

/// `GET /api/v1/memory/{id}` — the "why do I believe this?" provenance object (§11). This is also the
/// only endpoint that carries an experience's confidence and evidence count, which is why the detail
/// sheet asks for it rather than reading numbers the list never received.
private struct MemoryProvenance: Decodable {
    let content: String?
    let layer: String?
    let source: String?
    let confidence: Double?
    let evidenceCount: Int?
    let evidenceState: String?
    let verification: String?
    let taskId: String?
    let evidence: [Failable<Item>]?
    let research: [Failable<Research>]?
    let decisions: [Failable<Decision>]?

    var evidenceItems: [Item] { (evidence ?? []).compacted() }
    var researchItems: [Research] { (research ?? []).compacted() }
    var decisionItems: [Decision] { (decisions ?? []).compacted() }

    struct Item: Decodable, Identifiable {
        let source: String?
        let confidence: Double?
        let note: String?
        var id: String { "\(source ?? "")|\(note ?? "")|\(confidence ?? 0)" }
    }

    struct Research: Decodable, Identifiable {
        let query: String?
        let source: String?
        var id: String { "\(query ?? "")|\(source ?? "")" }
    }

    struct Decision: Decodable, Identifiable {
        let decision: String?
        let reason: String?
        var id: String { "\(decision ?? "")|\(reason ?? "")" }
    }

    enum CodingKeys: String, CodingKey {
        case content, layer, source, confidence
        case evidenceCount = "evidence_count", evidenceState = "evidence_state", verification
        case taskId = "task_id", evidence, research, decisions
    }
}

// MARK: - View model

@MainActor
fileprivate final class MemoryViewModel: ObservableObject {
    @Published var state: Loadable<Void> = .idle
    @Published var counts: MemoryCounts?
    @Published var recentExperiences: [MemoryExperience] = []
    @Published var conflicts: [MemoryConflictItem] = []
    /// Said once, quietly, when part of a load failed but the rest arrived.
    @Published private(set) var partialNote: String?

    @Published var searchText: String = ""
    @Published var searchResults: [MemoryExperience] = []
    @Published var isSearching = false
    @Published var searchError: String?

    /// The live answer to "what would Sali retrieve for this?" — transient by design (see `RecallResponse`).
    @Published var recallIntent: String?
    @Published var recalledMemories: [RecalledMemory] = []
    @Published var graphFacts: [GraphFact] = []
    @Published var isRecalling = false
    @Published var recallError: String?

    /// Teaching (`POST /api/v1/memory`) — controller-only on the host.
    @Published private(set) var isTeaching = false
    @Published var teachError: String?
    /// Set when the HOST answers 403 to a real attempt: the server is the authority on this device's
    /// role, not the token it cached. The control then retires quietly rather than every tap alerting.
    @Published private(set) var teachDenied = false
    /// A brief, self-clearing note that the host stored what was taught.
    @Published private(set) var confirmation: String?
    private var confirmationTask: Task<Void, Never>?

    @Published var provenance: Loadable<Void> = .idle
    @Published var provenanceDetail: MemoryProvenance?

    private var searchTask: Task<Void, Never>?

    var hasQuestion: Bool { !searchText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    /// Live triggers — the host telling this screen its answer changed. `memory.retrieved` is excluded
    /// deliberately: this screen's own recall emits it, so reacting to it would loop.
    static let liveTriggers: Set<EventType> = [
        .other(.experienceRecorded), .other(.experienceVerified), .other(.experienceLesson),
        .other(.memoryConsolidated),
    ]

    /// Debounced recall — triggered on every keystroke, but only actually hits the network ~350ms after
    /// typing pauses so the field feels responsive without hammering the backend per character. A blank
    /// or whitespace-only field is not a question: it clears what's on screen and sends nothing at all
    /// (the recall endpoint rightly answers 400 to an empty `q`, and an empty box deserves an empty
    /// answer).
    func scheduleSearch(api: APIClient) {
        searchTask?.cancel()
        let query = searchText
        guard !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            clearResults()
            return
        }
        searchTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 350_000_000)
            guard !Task.isCancelled, let self, self.searchText == query else { return }
            await self.search(api: api)
        }
    }

    /// Only the FIRST load is a loading state: a pull-to-refresh must not tear the screen down to a
    /// placeholder, and a transient failure on refresh keeps the last-known content. The two requests
    /// are also independent — conflicts failing costs the conflicts section, not the screen.
    func load(api: APIClient) async {
        if case .loaded = state {} else { state = .loading }

        async let overviewResponse: MemoryOverviewResponse = api.get("memory")
        async let conflictsResponse: MemoryConflictsResponse = api.get("memory/conflicts")

        var failures: [String] = []
        var firstMessage: String?

        do {
            let overview = try await overviewResponse
            counts = overview.counts
            recentExperiences = overview.experiences
        } catch {
            failures.append("what Sali remembers")
            firstMessage = Self.message(for: error)
        }

        do {
            conflicts = try await conflictsResponse.items
        } catch {
            failures.append("conflicts")
            if firstMessage == nil { firstMessage = Self.message(for: error) }
        }

        if failures.count == 2 {
            if case .loaded = state {
                partialNote = firstMessage ?? "Couldn't reach Sali just now — showing the last answer."
            } else {
                state = .failed(firstMessage ?? "Couldn't reach Sali.")
            }
            return
        }
        partialNote = failures.isEmpty
            ? nil
            : "Couldn't refresh \(failures.joined(separator: " and ")) just now — the rest is current."
        state = .loaded(())
    }

    /// One question, two honest answers: what Sali would *recall* (the retrieval the agent itself runs
    /// before it speaks) and which recorded experiences match. Asked together so the screen never shows
    /// half a picture, and never asked at all for a blank query.
    func search(api: APIClient) async {
        let question = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !question.isEmpty else { clearResults(); return }
        isRecalling = true
        isSearching = true
        recallError = nil
        searchError = nil

        async let recall: RecallResponse = api.get("recall", query: ["q": question])
        async let experiences: ExperiencesResponse = api.get("experiences", query: ["objective": question])

        do {
            let response = try await recall
            guard isCurrent(question) else { return }
            recallIntent = response.intent
            recalledMemories = response.recalled.filter { !($0.content ?? "").isEmpty }
            graphFacts = response.facts
        } catch {
            guard isCurrent(question) else { return }
            recallError = Self.message(for: error)
            recallIntent = nil
            recalledMemories = []
            graphFacts = []
        }
        isRecalling = false

        do {
            let response = try await experiences
            guard isCurrent(question) else { return }
            searchResults = response.items
        } catch {
            guard isCurrent(question) else { return }
            searchError = Self.message(for: error)
            searchResults = []
        }
        isSearching = false
    }

    /// A slow answer to an old question must never overwrite the answer to the current one.
    private func isCurrent(_ query: String) -> Bool {
        searchText.trimmingCharacters(in: .whitespacesAndNewlines) == query
    }

    func clearSearch() {
        searchTask?.cancel()
        searchText = ""
        clearResults()
    }

    /// Drop every result for the previous question. Recall mirrors the host's memory rather than caching
    /// it, so when the question goes away the answer goes with it.
    private func clearResults() {
        searchResults = []
        searchError = nil
        recallIntent = nil
        recalledMemories = []
        graphFacts = []
        recallError = nil
        isRecalling = false
        isSearching = false
    }

    /// `POST /api/v1/memory` — teach Sali a fact, the phone's `sali remember`. Content that is empty
    /// after trimming never leaves the device (the host answers 400), and the layer can only be one of
    /// the four this screen offers. A 403 means this device isn't a controller whatever role its token
    /// claims, so the control retires instead of the same alert appearing on every attempt.
    func teach(content: String, layer: MemoryLayer, api: APIClient) async -> Bool {
        let trimmed = content.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return false }
        isTeaching = true
        teachError = nil
        defer { isTeaching = false }
        do {
            let saved: TaughtMemory = try await api.post(
                "memory", json: ["content": trimmed, "layer": layer.rawValue])
            confirm("Sali will remember that — stored in \(saved.layer ?? layer.rawValue) memory.")
            await quietlyRefresh(api: api)
            return true
        } catch {
            if let apiError = error as? APIError, case .forbidden = apiError {
                teachDenied = true
                teachError = "Only a controller device can teach Sali new facts."
            } else {
                teachError = Self.message(for: error)
            }
            return false
        }
    }

    private func confirm(_ message: String) {
        confirmationTask?.cancel()
        confirmation = message
        confirmationTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 4_000_000_000)
            guard !Task.isCancelled else { return }
            self?.confirmation = nil
        }
    }

    /// After teaching, re-read the host so what's shown is its state rather than this screen's
    /// optimistic idea of it — including re-running the question on screen, since the new memory may now
    /// be recalled. Deliberately leaves `state` alone so the list doesn't flash back to a placeholder.
    private func quietlyRefresh(api: APIClient) async {
        if let overview: MemoryOverviewResponse = try? await api.get("memory") {
            counts = overview.counts
            recentExperiences = overview.experiences
        }
        if hasQuestion { await search(api: api) }
    }

    /// The provenance object also carries the confidence and evidence count the list endpoints never
    /// send, so this is what fills the "how sure, and why" half of the detail sheet.
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
            // A synthetic id may not resolve to a `memory` row (the endpoint takes a UUID) — degrade
            // quietly; what the sheet already shows covers the essentials.
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
    @ScaledMetric(relativeTo: .callout) private var glyphColumn: CGFloat = 22
    @StateObject private var viewModel = MemoryViewModel()
    @State private var selectedExperience: MemoryExperience?
    @State private var showTeachSheet = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// Both authorities have to agree before the control is live: the role this device holds, and the
    /// host's answer to a real attempt (which wins if it disagrees).
    private var canTeach: Bool { appState.role.canControl && !viewModel.teachDenied }

    // Pushed as a NavigationLink destination from MoreView, so — like NotificationsView — this does not
    // open its own NavigationStack; it shares the one already on screen.
    var body: some View {
        content
            .navigationTitle("Memory")
            .navigationBarTitleDisplayMode(.inline)
            .task { if case .idle = viewModel.state { await viewModel.load(api: appState.api) } }
            .refreshable { await viewModel.load(api: appState.api) }
            .onChange(of: appState.latestEvent?.id) { _, _ in
                guard let type = appState.latestEvent?.type,
                      MemoryViewModel.liveTriggers.contains(type) else { return }
                Task { await viewModel.load(api: appState.api) }
            }
            .sheet(item: $selectedExperience) { experience in
                ExperienceDetailSheet(experience: experience, viewModel: viewModel, api: appState.api)
            }
            .sheet(isPresented: $showTeachSheet, onDismiss: { viewModel.teachError = nil }) {
                TeachSaliSheet(viewModel: viewModel, api: appState.api)
            }
    }

    @ViewBuilder private var content: some View {
        switch viewModel.state {
        case .idle, .loading:
            // The counts header and the experience rows are exactly what resolves here, so the
            // placeholder is shaped like them rather than a spinner in the middle of nowhere (item 1).
            SaliSkeletonList(rows: 5, showsStats: true)
        case .failed(let message):
            ErrorStateView(message) { Task { await viewModel.load(api: appState.api) } }
        case .loaded:
            List {
                summarySection
                askSection
                if viewModel.hasQuestion {
                    recallSection
                    if !viewModel.graphFacts.isEmpty { graphSection }
                    searchResultsSection
                } else {
                    recentSection
                }
                if !viewModel.conflicts.isEmpty { conflictsSection }
                teachSection
            }
            .listStyle(.insetGrouped)
            .saliList()
            .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.standard),
                       value: viewModel.searchResults.count)
            .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.standard),
                       value: viewModel.recalledMemories.count)
            .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle),
                       value: viewModel.hasQuestion)
            .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick),
                       value: viewModel.confirmation)
        }
    }

    // MARK: sections

    /// The totals, then what they are made of. `by_layer` is the composition behind the headline number,
    /// so it sits directly under it rather than being decoded and thrown away.
    private var summarySection: some View {
        Section {
            HStack(alignment: .top, spacing: Theme.Spacing.l) {
                MetricTile(value: viewModel.counts?.total, label: "Memories")
                MetricTile(value: viewModel.counts?.experiences, label: "Experiences")
                MetricTile(value: viewModel.counts?.openConflicts, label: "Open conflicts",
                           tint: (viewModel.counts?.openConflicts ?? 0) > 0 ? Theme.Colors.warn : nil)
            }
            .padding(.vertical, Theme.Spacing.s)
            .frame(maxWidth: .infinity)

            ForEach(viewModel.counts?.layers ?? []) { layer in
                LayerCountRow(layer: layer, total: viewModel.counts?.total ?? 0)
            }

            // The way in to individual memories. This screen answers "what shaped Sali"; the browser
            // answers "what exactly does it believe, and is any of it wrong" — and is the only place
            // anything can be corrected, retired or erased.
            NavigationLink {
                MemoryBrowserView()
            } label: {
                Label("Browse and manage", systemImage: "list.bullet.rectangle")
                    .font(Theme.Typography.callout)
                    .frame(minHeight: 44)
            }
        } header: {
            SectionHeader("What Sali remembers",
                          subtitle: "Everything here carries where it came from, how sure Sali is, and how fresh it is.")
        } footer: {
            if let note = viewModel.partialNote {
                SectionFooter(note)
            } else {
                SectionFooter("Nothing is treated as fact without evidence behind it.")
            }
        }
    }

    /// The primary verb of this screen, so it sits directly under the summary. The field is an input
    /// well on the sunken ground the system reserves for them.
    private var askSection: some View {
        Section {
            HStack(spacing: Theme.Spacing.s) {
                Image(systemName: "magnifyingglass")
                    .font(Theme.Typography.callout)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .accessibilityHidden(true)
                TextField("Ask what Sali would recall", text: $viewModel.searchText)
                    .font(Theme.Typography.body)
                    .foregroundStyle(Theme.Colors.primaryText)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .submitLabel(.search)
                    .onSubmit { Task { await viewModel.search(api: appState.api) } }
                if !viewModel.searchText.isEmpty {
                    Button {
                        viewModel.clearSearch()
                    } label: {
                        // The glyph stays small; the hit area does not (item 4).
                        Image(systemName: "xmark.circle.fill")
                            .font(Theme.Typography.callout)
                            .foregroundStyle(Theme.Colors.tertiaryText)
                            .frame(width: 44, height: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Clear the question")
                }
            }
            .padding(.leading, Theme.Spacing.m)
            .padding(.trailing, viewModel.searchText.isEmpty ? Theme.Spacing.m : 0)
            .frame(minHeight: 44)
            .saliSurface(.sunken, radius: Theme.Radius.s)
            .listRowInsets(EdgeInsets(top: Theme.Spacing.xs, leading: Theme.Spacing.listMargin,
                                      bottom: Theme.Spacing.xs, trailing: Theme.Spacing.listMargin))
            .listRowBackground(Color.clear)
            .listRowSeparator(.hidden)
            .onChange(of: viewModel.searchText) { _, _ in
                viewModel.scheduleSearch(api: appState.api)
            }
        } header: {
            SectionHeader("Ask Sali",
                          subtitle: "The same retrieval it runs before it answers you — what comes back is what it would actually have in mind.",
                          emphasis: .secondary)
        }
    }

    /// What `sali recall` prints, honestly: every memory with the layer it lives in, how far Sali trusts
    /// it, and whether it has gone stale. The intent sits in the header because it explains *why* these
    /// particular memories came back.
    private var recallSection: some View {
        Section {
            if viewModel.isRecalling {
                // Settles into `RecalledMemoryRow`s, so wait in that shape rather than on a spinner.
                ForEach(0..<2, id: \.self) { _ in
                    SaliSkeletonRow(lines: 1, showsPill: true, showsBar: true)
                        .padding(.vertical, Theme.Spacing.xs)
                }
            } else if let error = viewModel.recallError {
                InlineError(error)
            } else if viewModel.recalledMemories.isEmpty {
                QuietNote("Sali wouldn't recall anything for that.")
            } else {
                ForEach(viewModel.recalledMemories) { RecalledMemoryRow(memory: $0) }
            }
        } header: {
            SectionHeader("What Sali would recall", subtitle: intentSubtitle)
        }
    }

    /// The host's own read of the question — shown subtly, because it's the reason the results look as
    /// they do rather than a result in its own right.
    private var intentSubtitle: String? {
        guard let intent = viewModel.recallIntent?.trimmingCharacters(in: .whitespacesAndNewlines),
              !intent.isEmpty else { return nil }
        return "Read as \(intent.replacingUnderscores)"
    }

    private var graphSection: some View {
        Section {
            ForEach(viewModel.graphFacts) { GraphFactRow(fact: $0) }
        } header: {
            SectionHeader("Connections", subtitle: "Links Sali drew between things it knows.",
                          emphasis: .secondary)
        } footer: {
            SectionFooter("Read as source, relationship, target.",
                          detail: "\(viewModel.graphFacts.count)")
        }
    }

    private var searchResultsSection: some View {
        Section {
            if viewModel.isSearching {
                ForEach(0..<2, id: \.self) { _ in
                    SaliSkeletonRow(lines: 2)
                        .padding(.vertical, Theme.Spacing.xs)
                }
            } else if let error = viewModel.searchError {
                InlineError(error)
            } else if viewModel.searchResults.isEmpty {
                QuietNote("Nothing Sali has done touches that yet.")
            } else {
                ForEach(viewModel.searchResults) { experience in
                    Button { selectedExperience = experience } label: {
                        ExperienceRow(experience: experience)
                    }
                    .buttonStyle(.plain)
                }
            }
        } header: {
            SectionHeader("Recorded experiences", subtitle: "Things Sali did that touch this.",
                          emphasis: .secondary)
        }
    }

    private var recentSection: some View {
        Section {
            if viewModel.recentExperiences.isEmpty {
                // No icon: the default is the Sali mark, which is the identity a generic glyph was
                // standing in for.
                EmptyStateView(title: "Nothing learned yet",
                               message: "As Sali completes tasks and verifies outcomes, the experiences that shaped its behavior will show up here.")
                    .listRowInsets(EdgeInsets())
                    .listRowBackground(Color.clear)
            } else {
                ForEach(viewModel.recentExperiences) { experience in
                    Button { selectedExperience = experience } label: {
                        ExperienceRow(experience: experience)
                    }
                    .buttonStyle(.plain)
                }
            }
        } header: {
            SectionHeader("Recent experiences", subtitle: "The most recently verified first.")
        }
    }

    private var conflictsSection: some View {
        Section {
            ForEach(viewModel.conflicts) { ConflictRow(conflict: $0) }
        } header: {
            SectionHeader("Conflicts", subtitle: "Where new evidence contradicted what Sali believed.")
        } footer: {
            SectionFooter("Recorded rather than silently overwritten, and resolved by which source is more trustworthy.",
                          detail: openConflictDetail)
        }
    }

    private var openConflictDetail: String {
        let open = viewModel.conflicts.filter(\.isOpen).count
        return open > 0 ? "\(open) open" : "none open"
    }

    /// The phone's `sali remember`, last on the screen because writing into Sali's memory is the
    /// consequential act here. Disabled rather than hidden when this device can't teach, with the reason
    /// said once and quietly — an observer learns why here, not one failed tap at a time.
    private var teachSection: some View {
        Section {
            Button { showTeachSheet = true } label: {
                HStack(spacing: Theme.Spacing.m) {
                    Image(systemName: "plus")
                        .font(Theme.Typography.callout.weight(.semibold))
                        .foregroundStyle(canTeach ? Theme.Colors.primaryText : Theme.Colors.tertiaryText)
                        .frame(width: glyphColumn)
                        .accessibilityHidden(true)
                    Text("Teach Sali a fact")
                        .font(Theme.Typography.body.weight(.medium))
                        .foregroundStyle(canTeach ? Theme.Colors.primaryText : Theme.Colors.tertiaryText)
                    Spacer(minLength: 0)
                }
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(!canTeach)

            if let confirmation = viewModel.confirmation {
                HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                    Image(systemName: "checkmark")
                        .font(Theme.Typography.caption.weight(.semibold))
                        .foregroundStyle(Theme.Colors.primaryText)
                        .frame(width: glyphColumn)
                        .accessibilityHidden(true)
                    Text(confirmation)
                        .font(Theme.Typography.footnote)
                        .foregroundStyle(Theme.Colors.secondaryText)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .frame(minHeight: 44, alignment: .leading)
                .transition(.opacity)
            }
        } header: {
            SectionHeader("Add to what Sali knows", emphasis: .secondary)
        } footer: {
            SectionFooter(teachGuidance)
        }
    }

    private var teachGuidance: String {
        if viewModel.teachDenied {
            return "The host declined that: only a controller device can add to Sali's memory."
        }
        return appState.role.canControl
            ? "What you teach goes straight into the memory Sali recalls from. How far it trusts it is Sali's call, not yours."
            : "Read-only on this device. Only a controller can teach Sali new facts."
    }
}

// MARK: - Shared pieces

/// A count and what it counts. Monospaced digits keep the three tiles' column edges still as the
/// numbers change; a missing count is an em-dash, never a zero Sali never claimed.
private struct MetricTile: View {
    let value: Int?
    let label: String
    var tint: Color?

    var body: some View {
        VStack(spacing: Theme.Spacing.xs) {
            Text(value.map(String.init) ?? "—")
                .font(Theme.Typography.numeral)
                .foregroundStyle(tint ?? ((value ?? 0) > 0 ? Theme.Colors.primaryText
                                                           : Theme.Colors.tertiaryText))
            Text(label)
                .font(Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.secondaryText)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity, alignment: .top)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(value.map(String.init) ?? "unknown") \(label)")
    }
}

/// One layer of memory and how much of it there is, with a hairline share bar so the composition is
/// legible without reading every number.
private struct LayerCountRow: View {
    let layer: MemoryLayerCount
    let total: Int
    @ScaledMetric(relativeTo: .footnote) private var barWidth: CGFloat = 64
    @ScaledMetric(relativeTo: .footnote) private var numberWidth: CGFloat = 28

    var body: some View {
        HStack(spacing: Theme.Spacing.m) {
            Text(layer.name.replacingUnderscores)
                .font(Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.primaryText)
            Spacer(minLength: Theme.Spacing.s)
            ConfidenceBar(share).frame(width: barWidth)
            Text("\(layer.count)")
                .font(Theme.Typography.numeralSmall)
                .foregroundStyle(Theme.Colors.tertiaryText)
                .frame(minWidth: numberWidth, alignment: .trailing)
        }
        .padding(.vertical, Theme.Spacing.s)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(layer.name.replacingUnderscores): \(layer.count)")
    }

    /// A share of the total, drawn on the same strip confidence uses so one bar means one thing.
    private var share: Double { total > 0 ? Double(layer.count) / Double(total) : 0 }
}

/// The one voice for "there is nothing here yet" inside a section.
private struct QuietNote: View {
    let text: String
    init(_ text: String) { self.text = text }

    var body: some View {
        Text(text)
            .font(Theme.Typography.footnote)
            .foregroundStyle(Theme.Colors.secondaryText)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .padding(.vertical, Theme.Spacing.xs)
    }
}

/// A failure the user should see, stated where it happened rather than over the whole screen.
private struct InlineError: View {
    let text: String
    init(_ text: String) { self.text = text }

    var body: some View {
        Text(text)
            .font(Theme.Typography.footnote)
            .foregroundStyle(Theme.Colors.danger)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .padding(.vertical, Theme.Spacing.xs)
    }
}

/// A row's bottom line: small, quiet, evenly spaced facts that never compete with the title above them.
private struct MetadataLine: View {
    let items: [String]

    var body: some View {
        if !items.isEmpty {
            HStack(spacing: Theme.Spacing.s) {
                ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                    if index > 0 {
                        Text("·")
                            .font(Theme.Typography.metadata)
                            .foregroundStyle(Theme.Colors.tertiaryText)
                            .accessibilityHidden(true)
                    }
                    Text(item)
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.tertiaryText)
                }
            }
            .accessibilityElement(children: .combine)
        }
    }
}

/// Confidence, as one measured strip and one number, at a width every row shares so the column reads
/// straight down the screen.
private struct ConfidenceReadout: View {
    let value: Double
    @ScaledMetric(relativeTo: .footnote) private var barWidth: CGFloat = 56

    var body: some View {
        HStack(spacing: Theme.Spacing.s) {
            ConfidenceBar(value).frame(width: barWidth)
            Text("\(percent)%")
                .font(Theme.Typography.numeralSmall)
                .foregroundStyle(Theme.Colors.secondaryText)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Confidence \(percent) percent")
    }

    private var percent: Int { Int((max(0, min(1, value)) * 100).rounded()) }
}

/// A label and its value, aligned on one column so a whole sheet of them reads as a table.
private struct DetailRow<Value: View>: View {
    let label: String
    @ViewBuilder var value: Value
    @ScaledMetric(relativeTo: .footnote) private var labelWidth: CGFloat = 116

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.l) {
            Text(label)
                .font(Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.secondaryText)
                .frame(width: labelWidth, alignment: .leading)
            value
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(minHeight: 44)
        .accessibilityElement(children: .combine)
    }
}

private extension DetailRow where Value == Text {
    init(_ label: String, _ text: String) {
        self.init(label: label) {
            Text(text)
                .font(Theme.Typography.body)
                .foregroundStyle(Theme.Colors.primaryText)
        }
    }
}

// MARK: - Rows

private struct ExperienceRow: View {
    let experience: MemoryExperience

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            Text(experience.title)
                .font(Theme.Typography.body.weight(.medium))
                .foregroundStyle(Theme.Colors.primaryText)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)

            if let detail = experience.detailText {
                Text(detail)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let outcome = experience.outcome, !outcome.isEmpty {
                Text(outcome)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .lineLimit(2)
            }

            HStack(alignment: .center, spacing: Theme.Spacing.m) {
                if let confidence = experience.confidence {
                    ConfidenceReadout(value: confidence)
                }
                if let state = experience.evidenceState, !state.isEmpty {
                    StatusPill(state.replacingUnderscores, color: evidenceInk(state))
                }
                Spacer(minLength: Theme.Spacing.s)
                MetadataLine(items: metadata)
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityHint("Opens the record behind this experience")
    }

    private var metadata: [String] {
        var items: [String] = []
        if let scope = experience.scopeMetadata { items.append(scope) }
        if !experience.steps.isEmpty { items.append("\(experience.steps.count) steps") }
        if let createdAt = experience.createdAt { items.append(createdAt.saliRelative) }
        return items
    }

    /// Evidence state is descriptive, not a call to action — only a contradiction actually wants the
    /// user, so only that one is allowed a hue. "Verified" earns weight, not colour.
    private func evidenceInk(_ state: String) -> Color {
        switch state {
        case "verified", "promoted": Theme.Colors.primaryText
        case "contradicted", "rejected": Theme.Colors.danger
        default: Theme.Colors.secondaryText
        }
    }
}

/// One memory as Sali would surface it: what it believes, where that belief lives, how far it trusts it,
/// and — said plainly rather than implied — whether it has gone stale. Confidence is drawn only when the
/// host sent one; nothing here is invented to fill a gap.
private struct RecalledMemoryRow: View {
    let memory: RecalledMemory

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            Text(memory.content ?? "")
                .font(Theme.Typography.body)
                .foregroundStyle(Theme.Colors.primaryText)
                .fixedSize(horizontal: false, vertical: true)

            HStack(alignment: .center, spacing: Theme.Spacing.m) {
                if let confidence = memory.confidence {
                    ConfidenceReadout(value: confidence)
                }
                if let layer = memory.layer, !layer.isEmpty {
                    StatusPill(layer.replacingUnderscores)
                }
                Spacer(minLength: Theme.Spacing.s)
                if memory.stale == true {
                    // Staleness is the one thing here that genuinely wants the reader.
                    Text("stale")
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.warn)
                        .accessibilityLabel("This memory has gone stale")
                }
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .frame(minHeight: 44, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

/// A graph edge, drawn the way it reads: `src —rel→ dst`.
private struct GraphFactRow: View {
    let fact: GraphFact

    var body: some View {
        (Text(fact.src ?? "—")
            .font(Theme.Typography.body)
            .foregroundStyle(Theme.Colors.primaryText)
         + Text("  \(fact.rel?.replacingUnderscores ?? "related to")  ")
            .font(Theme.Typography.monoSmall)
            .foregroundStyle(Theme.Colors.tertiaryText)
         + Text(fact.dst ?? "—")
            .font(Theme.Typography.body)
            .foregroundStyle(Theme.Colors.primaryText))
            .fixedSize(horizontal: false, vertical: true)
            .padding(.vertical, Theme.Spacing.xs)
            .frame(minHeight: 44, alignment: .leading)
            .accessibilityLabel("\(fact.src ?? "") \(fact.rel?.replacingUnderscores ?? "relates to") \(fact.dst ?? "")")
    }
}

private struct ConflictRow: View {
    let conflict: MemoryConflictItem

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                Text(headline)
                    .font(Theme.Typography.body.weight(.medium))
                    .foregroundStyle(Theme.Colors.primaryText)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: Theme.Spacing.s)
                // An OPEN contradiction is genuinely waiting on a human; a resolved one is history.
                StatusPill((conflict.status ?? "open").replacingUnderscores,
                           color: conflict.isOpen ? Theme.Colors.warn : Theme.Colors.secondaryText)
            }
            if let resolution = conflict.resolution, !resolution.isEmpty {
                Text(resolution)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let createdAt = conflict.createdAt {
                MetadataLine(items: [createdAt.saliRelative])
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .frame(minHeight: 44, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    private var headline: String {
        let new = conflict.newSource?.replacingUnderscores ?? "new evidence"
        let old = conflict.oldSource?.replacingUnderscores ?? "a prior belief"
        return "\(new) vs. \(old)"
    }
}

// MARK: - Detail sheet

/// Everything the host actually holds about one experience: what happened, how Sali did it, what went
/// wrong on the way, and — fetched separately, because it is the only endpoint that carries them — the
/// confidence and evidence that make it a belief rather than a note.
private struct ExperienceDetailSheet: View {
    let experience: MemoryExperience
    @ObservedObject var viewModel: MemoryViewModel
    let api: APIClient

    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                whatHappenedSection
                if !experience.steps.isEmpty || !experience.whatFailed.isEmpty { howSection }
                provenanceSection
            }
            .listStyle(.insetGrouped)
            .saliList()
            .navigationTitle("Experience")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                        .font(Theme.Typography.body)
                        .tint(Theme.Colors.primaryText)
                }
            }
            .task { await viewModel.loadProvenance(memoryId: experience.memoryId ?? "", api: api) }
        }
    }

    private var whatHappenedSection: some View {
        Section {
            VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                Text(experience.title)
                    .font(Theme.Typography.titleSmall)
                    .foregroundStyle(Theme.Colors.primaryText)
                    .fixedSize(horizontal: false, vertical: true)
                if let detail = experience.detailText {
                    Text(detail)
                        .font(Theme.Typography.callout)
                        .foregroundStyle(Theme.Colors.secondaryText)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, Theme.Spacing.s)
            if let outcome = experience.outcome, !outcome.isEmpty {
                DetailRow("Outcome", outcome)
            }
            if let scope = experience.scope, !scope.isEmpty {
                DetailRow(scope.contains("/") ? "Workspace" : "Reusable in", scope)
            }
            if let state = experience.evidenceState, !state.isEmpty {
                DetailRow(label: "Evidence") {
                    StatusPill(state.replacingUnderscores)
                }
            }
        } header: {
            SectionHeader("What happened")
        }
    }

    /// The procedure is a chain of tool names, and the failures are the attempts that didn't work. Both
    /// come from the same record; showing only the success would be a flattering half-truth.
    private var howSection: some View {
        Section {
            if !experience.steps.isEmpty {
                Text(experience.steps.joined(separator: "  →  "))
                    .font(Theme.Typography.monoSmall)
                    .foregroundStyle(Theme.Colors.primaryText)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(Theme.Spacing.m)
                    .saliSurface(.sunken, radius: Theme.Radius.s)
                    .padding(.vertical, Theme.Spacing.xs)
            }
            ForEach(experience.whatFailed) { failure in
                VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                    Text(failure.tool ?? "a tool")
                        .font(Theme.Typography.footnote.weight(.semibold))
                        .foregroundStyle(Theme.Colors.primaryText)
                    if let error = failure.error, !error.isEmpty {
                        Text(error)
                            .font(Theme.Typography.monoSmall)
                            .foregroundStyle(Theme.Colors.secondaryText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.vertical, Theme.Spacing.xs)
                .frame(minHeight: 44, alignment: .leading)
                .accessibilityElement(children: .combine)
            }
        } header: {
            SectionHeader("How Sali did it", emphasis: .secondary)
        } footer: {
            SectionFooter(experience.whatFailed.isEmpty
                          ? "The tools Sali used, in order."
                          : "The tools Sali used, and the attempts that failed on the way there.")
        }
    }

    private var provenanceSection: some View {
        Section {
            provenanceContent
        } header: {
            SectionHeader("Why Sali believes this",
                          subtitle: "Its source, how sure it is, and the evidence underneath.")
        }
    }

    @ViewBuilder private var provenanceContent: some View {
        switch viewModel.provenance {
        case .idle, .loading:
            // Resolves into labelled rows, so wait in that shape.
            ForEach(0..<3, id: \.self) { _ in
                SaliSkeletonRow(lines: 1, showsPill: false)
                    .padding(.vertical, Theme.Spacing.xs)
            }
        case .failed(let message):
            QuietNote(message)
        case .loaded:
            if let detail = viewModel.provenanceDetail {
                if let confidence = detail.confidence {
                    DetailRow(label: "Confidence") { ConfidenceReadout(value: confidence) }
                }
                if let layer = detail.layer, !layer.isEmpty {
                    DetailRow("Layer", layer.replacingUnderscores)
                }
                if let source = detail.source, !source.isEmpty {
                    DetailRow("Source", source.replacingUnderscores)
                }
                if let verification = detail.verification, !verification.isEmpty {
                    DetailRow("Verified by", verification.replacingUnderscores)
                }
                if let count = detail.evidenceCount {
                    DetailRow(label: "Evidence records") {
                        Text("\(count)")
                            .font(Theme.Typography.numeralSmall)
                            .foregroundStyle(Theme.Colors.primaryText)
                    }
                }
                ForEach(detail.evidenceItems) { item in
                    EvidenceRow(item: item)
                }
                ForEach(detail.decisionItems) { decision in
                    ProvenanceNote(title: decision.decision, detail: decision.reason)
                }
                ForEach(detail.researchItems) { research in
                    ProvenanceNote(title: research.query, detail: research.source)
                }
                if detail.confidence == nil, detail.layer == nil, detail.source == nil,
                   detail.evidenceItems.isEmpty {
                    QuietNote("No further provenance recorded.")
                }
            } else {
                QuietNote("No further provenance recorded.")
            }
        }
    }
}

private struct EvidenceRow: View {
    let item: MemoryProvenance.Item

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                Text((item.source ?? "evidence").replacingUnderscores)
                    .font(Theme.Typography.footnote.weight(.semibold))
                    .foregroundStyle(Theme.Colors.primaryText)
                Spacer(minLength: Theme.Spacing.s)
                if let confidence = item.confidence {
                    Text("\(Int((max(0, min(1, confidence)) * 100).rounded()))%")
                        .font(Theme.Typography.numeralSmall)
                        .foregroundStyle(Theme.Colors.tertiaryText)
                }
            }
            if let note = item.note, !note.isEmpty {
                Text(note)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .frame(minHeight: 44, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

/// A decision Sali made or a query it ran, as recorded — title above, its reason beneath.
private struct ProvenanceNote: View {
    let title: String?
    let detail: String?

    var body: some View {
        if let title, !title.isEmpty {
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                Text(title)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.primaryText)
                    .fixedSize(horizontal: false, vertical: true)
                if let detail, !detail.isEmpty {
                    Text(detail)
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.tertiaryText)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.vertical, Theme.Spacing.xs)
            .frame(minHeight: 44, alignment: .leading)
            .accessibilityElement(children: .combine)
        }
    }
}

// MARK: - Teach sheet

/// `sali remember`, on the phone. Content is a real multi-line field because a memory is a sentence, not
/// a keyword, and the layer picker offers only layers the host accepts — an invalid one can't be built
/// here. Save stays off until there is something to save, and a refusal from the host is shown right
/// where the attempt was made rather than as an alert.
private struct TeachSaliSheet: View {
    @ObservedObject var viewModel: MemoryViewModel
    let api: APIClient
    @Environment(\.dismiss) private var dismiss

    @State private var content = ""
    @State private var layer: MemoryLayer = .semantic

    private var isValid: Bool { !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("What should Sali remember?", text: $content, axis: .vertical)
                        .font(Theme.Typography.body)
                        .foregroundStyle(Theme.Colors.primaryText)
                        .lineLimit(4...10)
                        .padding(.vertical, Theme.Spacing.xs)
                } header: {
                    SectionHeader("The fact")
                } footer: {
                    SectionFooter("Write it the way you'd say it out loud. Sali stores it as given and works out for itself how much weight it carries.")
                }

                Section {
                    Picker("Layer", selection: $layer) {
                        ForEach(MemoryLayer.allCases) { option in
                            Text(option.title).tag(option)
                        }
                    }
                    .font(Theme.Typography.body)
                    .tint(Theme.Colors.secondaryText)
                    .frame(minHeight: 44)
                } header: {
                    SectionHeader("Where it belongs", emphasis: .secondary)
                } footer: {
                    SectionFooter(layer.explanation)
                }

                if let error = viewModel.teachError {
                    Section { InlineError(error) }
                }
            }
            .listStyle(.insetGrouped)
            .saliList()
            .navigationTitle("Teach Sali")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                        .font(Theme.Typography.body)
                        .tint(Theme.Colors.secondaryText)
                }
                ToolbarItem(placement: .confirmationAction) {
                    if viewModel.isTeaching {
                        ProgressView().controlSize(.small).tint(Theme.Colors.secondaryText)
                    } else {
                        Button("Save") {
                            Task {
                                if await viewModel.teach(content: content, layer: layer, api: api) {
                                    dismiss()
                                }
                            }
                        }
                        .font(Theme.Typography.body.weight(.semibold))
                        .tint(Theme.Colors.primaryText)
                        .disabled(!isValid)
                    }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }
}

private extension String {
    var replacingUnderscores: String { replacingOccurrences(of: "_", with: " ") }
    /// Newlines and runs of spaces become one space, so a stored prompt reads as a sentence in a row.
    var collapsedWhitespace: String { split(whereSeparator: \.isWhitespace).joined(separator: " ") }
}

private func firstNonEmpty(_ candidates: String?...) -> String? {
    for candidate in candidates {
        if let candidate, !candidate.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return candidate
        }
    }
    return nil
}
