import Foundation
import SwiftUI

// Learning — the central question is "what can Sali do now that it couldn't before?" (§17). This screen
// makes learning visible as capability: the abilities Sali has actually acquired and is reaching for
// (use_count/last_used, not dead entries), the evidence-backed lessons underneath them, the
// contradictions in what it has learned, and the pending behavior proposals where the user is the final
// authority (§42) before anything changes how Sali works.
//
// Reading order is deliberate: what needs YOU first (proposals, contradictions), then what Sali can do,
// then the evidence that earned it. Nothing here is decorated with colour — rank, weight and spacing
// carry the hierarchy, and a hue appears only where a state genuinely wants a person.

// MARK: - Forgiving decode

/// One bad element must degrade itself, never the array around it. A single unparseable row (a date the
/// host formatted differently, a field that arrived as a list instead of a string) has blanked whole
/// screens in this app before; wrapping each element makes that failure local.
private struct Failable<Wrapped: Decodable>: Decodable {
    let value: Wrapped?
    init(from decoder: Decoder) throws { value = try? Wrapped(from: decoder) }
}

/// Spelled out because a generic type gets no implicit `Sendable` — and every response below stores
/// `[Failable<…>]`, so without this they cannot cross back from the `APIClient` actor.
extension Failable: Sendable where Wrapped: Sendable {}

private extension Array {
    /// `[Failable<T>]` → `[T]`, dropping only the elements that could not be read.
    func compacted<T>() -> [T] where Element == Failable<T> { compactMap(\.value) }
}

/// A timestamp that degrades to "unknown" instead of throwing. `APIClient.decoder` understands only
/// RFC-3339 with an offset; everything the host might legitimately send (a timezone-naive value, a
/// space-separated one, a two-digit Postgres offset, a bare epoch) is understood here, and anything left
/// over becomes `nil` — one unreadable date costs its own field and nothing more.
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
        // Postgres can render an offset as `+00`; RFC-3339 wants `+00:00`.
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
            formatter.timeZone = TimeZone(secondsFromGMT: 0)   // naive values are UTC on this host
            formatter.dateFormat = format
            if let date = formatter.date(from: trimmed) { return date }
        }
        return nil
    }
}

// MARK: - Wire shapes (`src/sali/api/routes/api.py`)

/// `GET /api/v1/learning` → `{ candidates: {...counts}, behavior: {...counts} }`.
private struct LearningHealth: Decodable {
    let candidates: LearningCandidateCounts?
    let behavior: BehaviorCounts?
}

private struct LearningCandidateCounts: Decodable {
    let active: Int?
    let observed: Int?
    let verified: Int?
    let promoted: Int?
    let openContradictions: Int?
    enum CodingKeys: String, CodingKey {
        case active, observed, verified, promoted, openContradictions = "open_contradictions"
    }
}

private struct BehaviorCounts: Decodable {
    let pending: Int?
    let accepted: Int?
    let rejected: Int?
}

/// `GET /api/v1/learning/candidates` → verified/promoted/supported lessons, best evidence first
/// (`src/sali/learning/candidates.py: active()`).
private struct LearningCandidatesResponse: Decodable {
    let candidates: [Failable<LearningCandidateItem>]?
    var items: [LearningCandidateItem] { (candidates ?? []).compacted() }
}

private struct LearningCandidateItem: Decodable, Identifiable {
    let candidateId: String?
    let lesson: String?
    let scope: String?
    let scopeRef: String?
    let source: String?
    let sourceType: String?
    let evidenceLevel: Int?
    let confidence: Double?
    let timesSuccessful: Int?
    let timesFailed: Int?
    let verificationState: String?
    let promoted: Bool?
    let updated: WireDate?

    /// Stable for the lifetime of the row. A computed `UUID()` fallback re-identifies the row on every
    /// render, which tears `ForEach` diffing apart; this one is derived from content instead.
    var id: String { candidateId ?? "lesson|\(lesson ?? "")|\(scope ?? "")" }
    var updatedAt: Date? { updated?.value }

    enum CodingKeys: String, CodingKey {
        case candidateId = "id", lesson, scope, scopeRef = "scope_ref", source
        case sourceType = "source_type", evidenceLevel = "evidence_level", confidence
        case timesSuccessful = "times_successful", timesFailed = "times_failed"
        case verificationState = "verification_state", promoted, updated = "updated_at"
    }
}

/// `GET /api/v1/learning/contradictions` → the knowledge contradictions Sali recorded rather than
/// silently resolving (`candidates.py: contradictions()`). The headline count comes from `/learning`;
/// this is the list behind it, so the number is never a dead end.
private struct LearningContradictionsResponse: Decodable {
    let contradictions: [Failable<LearningContradictionItem>]?
    var items: [LearningContradictionItem] { (contradictions ?? []).compacted() }
}

private struct LearningContradictionItem: Decodable, Identifiable {
    let contradictionId: String?
    let scope: String?
    let claimKey: String?
    let oldClaim: String?
    let newClaim: String?
    let status: String?
    let resolution: String?
    let created: WireDate?

    var id: String { contradictionId ?? "contradiction|\(claimKey ?? "")|\(oldClaim ?? "")" }
    var createdAt: Date? { created?.value }
    /// `counts()` treats both of these as still open, so this screen has to as well.
    var isOpen: Bool { ["open", "needs_review"].contains(status ?? "open") }

    enum CodingKeys: String, CodingKey {
        case contradictionId = "id", scope, claimKey = "claim_key", oldClaim = "old_claim"
        case newClaim = "new_claim", status, resolution, created = "created_at"
    }
}

/// `GET /api/v1/capabilities` → `{ capabilities: [...], counts: {...} }`
/// (`src/sali/learning/capability.py: list()`). Decoded here rather than through the shared
/// `Capability` model so the confidence and success/failure record the host actually sends are not
/// thrown away, and so a capability row missing a name degrades instead of failing the response.
private struct CapabilitiesResponse: Decodable {
    let capabilities: [Failable<LearnedCapability>]?
    let counts: CapabilityCounts?
    var items: [LearnedCapability] { (capabilities ?? []).compacted() }
}

private struct CapabilityCounts: Decodable {
    let total: Int?
    let usable: Int?
}

/// `GET /api/v1/capability-acquisitions` → the gaps Sali is actively closing
/// (`src/sali/learning/capability_acquisition.py: open()`). This is the half of learning that has NOT
/// happened yet: something he decided he needs and is working through, stage by stage. It matters on
/// this screen because a list of finished capabilities only ever shows the past.
private struct AcquisitionsResponse: Decodable {
    let inProgress: [Failable<AcquisitionItem>]?
    var items: [AcquisitionItem] { (inProgress ?? []).compacted() }

    enum CodingKeys: String, CodingKey { case inProgress = "in_progress" }
}

private struct AcquisitionItem: Decodable, Identifiable {
    let acquisitionId: String?
    let capabilityName: String?
    let status: String?
    let started: WireDate?

    var id: String { acquisitionId ?? "acquisition|\(capabilityName ?? "")" }
    var startedAt: Date? { started?.value }

    /// Until the work succeeds the row is named after the objective that opened it — the reusable skill
    /// has no name yet — so this is a sentence, not a title, and is shown as one.
    var displayName: String { (capabilityName ?? "something new").replacingUnderscores }

    /// The stages the host records, in order. `blocked` is not a stage: it is a stall wherever it
    /// happened, so it keeps the stage it stalled at rather than pretending to be further along.
    static let stages = ["gap_identified", "researching", "acquiring", "verifying"]
    var isBlocked: Bool { status == "blocked" }
    var stageIndex: Int { Self.stages.firstIndex(of: status ?? "") ?? 0 }
}

/// `GET /api/v1/capabilities/detail` → one capability and the evidence under it. `availability` is the
/// host's honest answer to "can Sali do this *today*": 'available' only when it is both usable and
/// recently verified, 'stale' when it has not been proven in a while, 'degraded' when it has been
/// failing. The usage log is what separates a competence from a claim.
private struct CapabilityDetailResponse: Decodable {
    let availability: String?
    let usableNow: Bool?
    let usage: [Failable<CapabilityUse>]?
    var uses: [CapabilityUse] { (usage ?? []).compacted() }

    enum CodingKeys: String, CodingKey { case availability, usableNow = "usable_now", usage }
}

private struct CapabilityUse: Decodable, Identifiable {
    let action: String?
    let result: String?
    let success: Bool?
    let verified: Bool?
    let at: WireDate?

    var id: String { "\(at?.value?.timeIntervalSince1970 ?? 0)|\(action ?? "")" }
    var usedAt: Date? { at?.value }
    var worked: Bool { success ?? true }

    enum CodingKeys: String, CodingKey { case action, result, success, verified, at = "created_at" }
}

private struct LearnedCapability: Decodable, Identifiable {
    let capabilityId: String?
    let name: String?
    let status: String?
    let scope: String?
    let scopeRef: String?
    let purpose: String?
    let confidence: Double?
    let timesSucceeded: Int?
    let timesFailed: Int?
    let useCount: Int?
    let used: WireDate?

    var id: String { capabilityId ?? "capability|\(name ?? "")" }
    var lastUsed: Date? { used?.value }
    var displayName: String { (name ?? "capability").replacingUnderscores }

    /// Where this was learned, in words a person can use. The host scopes a capability to whatever the
    /// work touched — usually a directory, but sometimes an opaque workspace id. Spelled out in full, a
    /// UUID pushed every other fact on the row into a stack of single characters, and it told the reader
    /// nothing anyway, so it is dropped rather than shown.
    var placeLabel: String? {
        guard let reference = scopeRef, !reference.isEmpty else { return nil }
        let name = (reference as NSString).lastPathComponent
        let label = name.isEmpty ? reference : name
        // The test has to be on the label actually shown, not on the raw reference: the reference is
        // usually a path, and it is its LAST component that turns out to be the workspace id.
        return UUID(uuidString: label) == nil ? label : nil
    }

    enum CodingKeys: String, CodingKey {
        case capabilityId = "id", name, status, scope, scopeRef = "scope_ref", purpose, confidence
        case timesSucceeded = "times_succeeded", timesFailed = "times_failed"
        case useCount = "use_count", used = "last_used"
    }
}

/// `GET /api/v1/capabilities/overview` — built-in tool groups merged with the learned capabilities
/// (`src/sali/runtime/cognitive.py: capability_overview()`). Only the `builtin` half is read here: the
/// `learned` half is the very same rows as `/capabilities`, and showing them twice said nothing new.
private struct CapabilityOverview: Decodable {
    let builtin: [Failable<BuiltinCapabilityGroup>]?
    var groups: [BuiltinCapabilityGroup] { (builtin ?? []).compacted() }
}

private struct BuiltinCapabilityGroup: Decodable, Identifiable {
    let capability: String?
    let status: String?
    let tools: [String]?

    var id: String { capability ?? "group" }
    var displayName: String { (capability ?? "capability").replacingUnderscores }
    var toolNames: [String] { tools ?? [] }
}

/// `GET /api/v1/behavior/proposals` → the pending proposals (`src/sali/learning/behavior.py: pending()`).
private struct BehaviorProposalsResponse: Decodable {
    let proposals: [Failable<BehaviorProposalItem>]?
    var items: [BehaviorProposalItem] { (proposals ?? []).compacted() }
}

private struct BehaviorProposalItem: Decodable, Identifiable {
    /// Not optional: `POST /behavior/proposals/{id}/approve` needs it, so a proposal without one cannot
    /// be decided on and is dropped rather than rendered as two buttons that do nothing.
    let proposalId: String
    let scope: String?
    let trigger: String?
    let proposedBehavior: String?
    let reason: String?
    let confidence: Double?
    let timesObserved: Int?
    let status: String?
    let created: WireDate?

    var id: String { proposalId }
    var createdAt: Date? { created?.value }

    enum CodingKeys: String, CodingKey {
        case proposalId = "id", scope, trigger, proposedBehavior = "proposed_behavior", reason
        case confidence, timesObserved = "times_observed", status, created = "created_at"
    }
}

// MARK: - View model

@MainActor
fileprivate final class LearningViewModel: ObservableObject {
    @Published var state: Loadable<Void> = .idle
    @Published var candidates: [LearningCandidateItem] = []
    @Published var contradictions: [LearningContradictionItem] = []
    @Published var capabilities: [LearnedCapability] = []
    @Published var acquisitions: [AcquisitionItem] = []
    @Published var builtinGroups: [BuiltinCapabilityGroup] = []
    @Published var proposals: [BehaviorProposalItem] = []

    /// Set when part of a load failed but the rest arrived — said once, quietly, instead of throwing the
    /// whole screen away for one endpoint.
    @Published private(set) var partialNote: String?
    @Published var workingProposalId: String?
    /// Keyed by proposal so a refusal is shown on the row where the attempt was made, not as an alert
    /// over the whole screen.
    @Published var actionErrors: [String: String] = [:]

    private var candidateCounts: LearningCandidateCounts?
    private var behaviorCounts: BehaviorCounts?
    private var capabilityCounts: CapabilityCounts?

    var activeLearningCount: Int { candidateCounts?.active ?? candidates.count }
    var usableCapabilityCount: Int { capabilityCounts?.usable ?? capabilities.count }
    var totalCapabilityCount: Int { capabilityCounts?.total ?? capabilities.count }
    var pendingProposalCount: Int { behaviorCounts?.pending ?? proposals.count }
    var openContradictionCount: Int {
        candidateCounts?.openContradictions ?? contradictions.filter(\.isOpen).count
    }
    var acceptedProposalCount: Int { behaviorCounts?.accepted ?? 0 }

    /// Live triggers — the host telling this screen its answer changed. `capability.used` is excluded on
    /// purpose: it fires constantly during ordinary work and would turn a calm screen into a strobe.
    static let liveTriggers: Set<EventType> = [
        .other(.learningVerified), .other(.learningPromoted), .other(.learningRejected),
        .other(.learningContradicted), .other(.learningSuperseded),
        .other(.behaviorProposed), .other(.behaviorAccepted), .other(.behaviorRejected),
        .other(.behaviorSuperseded),
        .other(.capabilityDiscovered), .other(.capabilityVerified), .other(.capabilityDegraded),
        // The arc moves through these two, and it is the one thing on this screen that changes while
        // the user is watching. `capability.used` stays excluded — it fires constantly during ordinary
        // work and would strobe a calm screen.
        .other(.capabilityAttempted), .other(.capabilityFailed),
    ]

    /// Five endpoints, five independent answers. Only the FIRST load shows a loading state, and a
    /// refresh never tears down what is already on screen; one endpoint failing costs its own section
    /// rather than the screen, and only a total failure on a first load becomes an error state.
    func load(api: APIClient) async {
        if case .loaded = state {} else { state = .loading }

        async let healthResponse: LearningHealth = api.get("learning")
        async let candidatesResponse: LearningCandidatesResponse = api.get("learning/candidates")
        async let contradictionsResponse: LearningContradictionsResponse =
            api.get("learning/contradictions")
        async let capabilitiesResponse: CapabilitiesResponse = api.get("capabilities")
        async let overviewResponse: CapabilityOverview = api.get("capabilities/overview")
        async let proposalsResponse: BehaviorProposalsResponse = api.get("behavior/proposals")
        async let acquisitionsResponse: AcquisitionsResponse = api.get("capability-acquisitions")

        var failures: [String] = []
        var firstMessage: String?
        func note(_ label: String, _ error: Error) {
            failures.append(label)
            if firstMessage == nil { firstMessage = Self.message(for: error) }
        }

        do {
            let health = try await healthResponse
            candidateCounts = health.candidates
            behaviorCounts = health.behavior
        } catch { note("counts", error) }

        do { candidates = try await candidatesResponse.items } catch { note("lessons", error) }
        do { contradictions = try await contradictionsResponse.items.sortedByUrgency() }
        catch { note("contradictions", error) }

        do {
            let response = try await capabilitiesResponse
            capabilities = response.items
            capabilityCounts = response.counts
        } catch { note("capabilities", error) }

        do { acquisitions = try await acquisitionsResponse.items } catch { note("what it's learning", error) }
        do { builtinGroups = try await overviewResponse.groups } catch { note("self-description", error) }
        do { proposals = try await proposalsResponse.items } catch { note("proposals", error) }

        if failures.count == 7 {
            // Nothing came back at all. A first load says so; a refresh keeps the last-known screen.
            if case .loaded = state {
                partialNote = firstMessage ?? "Couldn't reach Sali just now — showing the last answer."
            } else {
                state = .failed(firstMessage ?? "Couldn't reach Sali.")
            }
            return
        }
        partialNote = failures.isEmpty
            ? nil
            : "Couldn't refresh \(failures.formattedList) just now — the rest is current."
        state = .loaded(())
    }

    func approve(_ proposal: BehaviorProposalItem, api: APIClient) async {
        await act(on: proposal, path: "approve", api: api)
    }

    func reject(_ proposal: BehaviorProposalItem, api: APIClient) async {
        await act(on: proposal, path: "reject", api: api)
    }

    /// The user is the final authority on behavior (§42). The decision is sent to the host and the row
    /// leaves only once the host has taken it — never optimistically.
    private func act(on proposal: BehaviorProposalItem, path: String, api: APIClient) async {
        workingProposalId = proposal.id
        actionErrors[proposal.id] = nil
        defer { workingProposalId = nil }
        do {
            try await api.postVoid("behavior/proposals/\(proposal.id)/\(path)")
            proposals.removeAll { $0.id == proposal.id }
            actionErrors[proposal.id] = nil
            // The decision changes the counts the headline is drawn from, so re-read them quietly.
            if let health: LearningHealth = try? await api.get("learning") {
                candidateCounts = health.candidates
                behaviorCounts = health.behavior
            }
        } catch {
            actionErrors[proposal.id] = Self.message(for: error)
        }
    }

    private static func message(for error: Error) -> String {
        (error as? APIError)?.errorDescription ?? "Couldn't reach Sali."
    }
}

private extension Array where Element == LearningContradictionItem {
    /// Still-open contradictions first — they are the ones asking for a person.
    func sortedByUrgency() -> [Element] {
        enumerated().sorted { lhs, rhs in
            if lhs.element.isOpen != rhs.element.isOpen { return lhs.element.isOpen }
            return lhs.offset < rhs.offset
        }.map(\.element)
    }
}

private extension Array where Element == String {
    var formattedList: String {
        switch count {
        case 0: ""
        case 1: self[0]
        case 2: "\(self[0]) and \(self[1])"
        default: "\(dropLast().joined(separator: ", ")), and \(self[count - 1])"
        }
    }
}

// MARK: - View

struct LearningView: View {
    @EnvironmentObject var appState: AppState
    @StateObject private var viewModel = LearningViewModel()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    // Pushed as a NavigationLink destination from MoreView, so — like NotificationsView — this does not
    // open its own NavigationStack; it shares the one already on screen.
    var body: some View {
        content
            .navigationTitle("Learning")
            .navigationBarTitleDisplayMode(.inline)
            .task { if case .idle = viewModel.state { await viewModel.load(api: appState.api) } }
            .refreshable { await viewModel.load(api: appState.api) }
            .onChange(of: appState.latestEvent?.id) { _, _ in
                guard let type = appState.latestEvent?.type,
                      LearningViewModel.liveTriggers.contains(type) else { return }
                Task { await viewModel.load(api: appState.api) }
            }
    }

    @ViewBuilder private var content: some View {
        switch viewModel.state {
        case .idle, .loading:
            // What resolves here is a counts header followed by rows carrying a status pill and a
            // confidence bar, so the placeholder is shaped like that rather than a spinner (item 1).
            SaliSkeletonList(rows: 4, showsStats: true, showsBar: true)
        case .failed(let message):
            ErrorStateView(message) { Task { await viewModel.load(api: appState.api) } }
        case .loaded:
            List {
                headlineSection
                if !viewModel.proposals.isEmpty { proposalsSection }
                if !viewModel.contradictions.isEmpty { contradictionsSection }
                if !viewModel.acquisitions.isEmpty { acquiringSection }
                capabilitiesSection
                builtinSection
                lessonsSection
            }
            .listStyle(.insetGrouped)
            .saliList()
            .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.standard),
                       value: viewModel.proposals.count)
            .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle),
                       value: viewModel.capabilities.count)
            .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle),
                       value: viewModel.acquisitions.count)
        }
    }

    // MARK: sections

    private var headlineSection: some View {
        Section {
            HStack(alignment: .top, spacing: Theme.Spacing.l) {
                MetricTile(value: viewModel.usableCapabilityCount, label: "Usable",
                           detail: viewModel.totalCapabilityCount > viewModel.usableCapabilityCount
                               ? "of \(viewModel.totalCapabilityCount)" : nil)
                MetricTile(value: viewModel.activeLearningCount, label: "Verified lessons")
                MetricTile(value: viewModel.pendingProposalCount, label: "Awaiting you",
                           detail: viewModel.acceptedProposalCount > 0
                               ? "\(viewModel.acceptedProposalCount) adopted" : nil)
            }
            .padding(.vertical, Theme.Spacing.s)
            .frame(maxWidth: .infinity)
        } header: {
            SectionHeader("What can Sali do now that it couldn't before?",
                          subtitle: "Counted from what the host has verified — never from what it was told.")
        } footer: {
            if let note = viewModel.partialNote {
                SectionFooter(note, detail: contradictionDetail)
            } else if viewModel.openContradictionCount > 0 {
                SectionFooter("Some of what Sali learned now disagrees with itself.",
                              detail: contradictionDetail)
            }
        }
    }

    /// Never let the count from `/learning` sit somewhere its list isn't.
    private var contradictionDetail: String? {
        viewModel.openContradictionCount > 0
            ? "\(viewModel.openContradictionCount) contradiction\(viewModel.openContradictionCount == 1 ? "" : "s") open"
            : nil
    }

    private var proposalsSection: some View {
        Section {
            ForEach(viewModel.proposals) { proposal in
                ProposalRow(proposal: proposal,
                            isWorking: viewModel.workingProposalId == proposal.id,
                            canDecide: appState.role.canControl,
                            error: viewModel.actionErrors[proposal.id]) {
                    Task { await viewModel.approve(proposal, api: appState.api) }
                } onReject: {
                    Task { await viewModel.reject(proposal, api: appState.api) }
                }
            }
        } header: {
            SectionHeader("Waiting on you",
                          subtitle: "Patterns Sali noticed and would like to adopt.")
        } footer: {
            SectionFooter("Changes to how Sali behaves are never automatic — you decide.",
                          detail: "\(viewModel.pendingProposalCount) pending")
        }
    }

    private var contradictionsSection: some View {
        Section {
            ForEach(viewModel.contradictions) { ContradictionRow(contradiction: $0) }
        } header: {
            SectionHeader("Contradictions",
                          subtitle: "Where new evidence disagreed with something Sali had learned.")
        } footer: {
            SectionFooter("Recorded for review rather than silently resolved.",
                          detail: viewModel.openContradictionCount > 0
                              ? "\(viewModel.openContradictionCount) open" : "none open")
        }
    }

    private var acquiringSection: some View {
        Section {
            ForEach(viewModel.acquisitions) { AcquisitionRow(acquisition: $0) }
        } header: {
            SectionHeader("Learning right now",
                          subtitle: "Gaps Sali hit in real work and is closing — where each one has got to.")
        } footer: {
            SectionFooter("A gap opens when a task needs something he can't already do, and closes only when the work proves it.")
        }
    }

    private var capabilitiesSection: some View {
        Section {
            if viewModel.capabilities.isEmpty {
                QuietNote("Nothing learned yet — capabilities appear once Sali picks up and verifies a new skill.")
            } else {
                ForEach(viewModel.capabilities) { capability in
                    NavigationLink { CapabilityDetailView(capability: capability) }
                        label: { CapabilityRow(capability: capability) }
                }
            }
        } header: {
            SectionHeader("Learned capabilities",
                          subtitle: "Skills Sali acquired itself, with the record that earned them.")
        } footer: {
            SectionFooter("Use counts show these are actually being reached for — not shelved once learned.")
        }
    }

    private var builtinSection: some View {
        Section {
            if viewModel.builtinGroups.isEmpty {
                QuietNote("Sali's self-description isn't available right now.")
            } else {
                ForEach(viewModel.builtinGroups) { BuiltinGroupRow(group: $0) }
            }
        } header: {
            SectionHeader("Built in",
                          subtitle: "The tool groups actually registered on the host right now.", emphasis: .secondary)
        } footer: {
            SectionFooter("Only groups whose tools are really loaded are listed — nothing claims an ability it doesn't have.")
        }
    }

    private var lessonsSection: some View {
        Section {
            if viewModel.candidates.isEmpty {
                QuietNote("No verified lessons yet.")
            } else {
                ForEach(viewModel.candidates) { LessonRow(candidate: $0) }
            }
        } header: {
            SectionHeader("Verified lessons",
                          subtitle: "Best evidence first.", emphasis: .secondary)
        } footer: {
            SectionFooter("Only evidence-backed lessons appear here — an untested idea never becomes a fact.")
        }
    }
}

// MARK: - Shared pieces

/// A count and what it counts. The numeral is monospaced so the three tiles keep their baselines and
/// column edges as the numbers change; a zero is drawn in the quietest ink there is, because "none" is
/// information but not news.
private struct MetricTile: View {
    let value: Int
    let label: String
    var detail: String?

    var body: some View {
        VStack(spacing: Theme.Spacing.xs) {
            Text("\(value)")
                .font(Theme.Typography.numeral)
                .foregroundStyle(value > 0 ? Theme.Colors.primaryText : Theme.Colors.tertiaryText)
            Text(label)
                .font(Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.secondaryText)
                .multilineTextAlignment(.center)
            if let detail, !detail.isEmpty {
                Text(detail)
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .multilineTextAlignment(.center)
            }
        }
        .frame(maxWidth: .infinity, alignment: .top)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(value) \(label)")
    }
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
                        // Without this a long item wraps INSIDE itself — "worked" broke across two
                        // lines as "worke / d" — and one over-long fact turns the row into a column.
                        .lineLimit(1)
                        .truncationMode(.tail)
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
            Text("\(Int((max(0, min(1, value)) * 100).rounded()))%")
                .font(Theme.Typography.numeralSmall)
                .foregroundStyle(Theme.Colors.secondaryText)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Confidence \(Int((max(0, min(1, value)) * 100).rounded())) percent")
    }
}

/// A quiet, monochrome counterpart to `SaliPrimaryButtonStyle` — the same shape and tempo, stated as an
/// outline. The system `.bordered` style tints itself with the accent colour, which this app doesn't have.
private struct SaliQuietButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View { Content(configuration: configuration) }

    struct Content: View {
        let configuration: ButtonStyleConfiguration
        @Environment(\.isEnabled) private var isEnabled
        @Environment(\.accessibilityReduceMotion) private var reduceMotion

        var body: some View {
            configuration.label
                .font(Theme.Typography.body.weight(.medium))
                .foregroundStyle(Theme.Colors.primaryText)
                .padding(.vertical, Theme.Spacing.m)
                .padding(.horizontal, Theme.Spacing.l)
                .frame(minHeight: 44)
                .background(Theme.Colors.surface)
                .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.m, style: .continuous))
                .saliHairline(radius: Theme.Radius.m, color: Theme.Colors.borderStrong)
                .opacity(isEnabled ? (configuration.isPressed ? 0.86 : 1) : 0.35)
                .scaleEffect(configuration.isPressed ? 0.99 : 1)
                .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick),
                           value: configuration.isPressed)
                .contentShape(Rectangle())
        }
    }
}

// MARK: - Rows

private struct AcquisitionRow: View {
    let acquisition: AcquisitionItem

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                Text(acquisition.displayName)
                    .font(Theme.Typography.body.weight(.medium))
                    .foregroundStyle(Theme.Colors.primaryText)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: Theme.Spacing.s)
                if acquisition.isBlocked {
                    StatusPill("blocked", color: Theme.Colors.warn)
                }
            }
            StageTrack(stage: acquisition.stageIndex, blocked: acquisition.isBlocked)
            if let started = acquisition.startedAt {
                Text("started \(started.saliRelative)")
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.tertiaryText)
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .frame(minHeight: 44, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

/// The four stages the host actually records, drawn as a track. Deliberately NOT a percentage: there is
/// no way to know how far into "practising" something is, and inventing a number would be the screen
/// making a claim the host never made. It shows which stage is live and which are behind it.
private struct StageTrack: View {
    let stage: Int
    let blocked: Bool

    private static let labels = ["gap", "reading up", "practising", "verifying"]

    var body: some View {
        HStack(alignment: .top, spacing: Theme.Spacing.xs) {
            ForEach(Array(Self.labels.enumerated()), id: \.offset) { index, label in
                VStack(alignment: .leading, spacing: 5) {
                    Capsule()
                        .fill(index <= stage ? ink : Theme.Colors.separator)
                        .frame(height: 3)
                    Text(label)
                        .font(Theme.Typography.caption)
                        .foregroundStyle(index == stage ? Theme.Colors.secondaryText
                                                        : Theme.Colors.tertiaryText)
                        .lineLimit(1)
                        .minimumScaleFactor(0.8)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(blocked
            ? "Stalled at \(Self.labels[min(stage, Self.labels.count - 1)])"
            : "Stage \(stage + 1) of 4, \(Self.labels[min(stage, Self.labels.count - 1)])")
    }

    private var ink: Color { blocked ? Theme.Colors.warn : Theme.Colors.accent }
}

/// One capability, and the record that earns it. The question this screen answers is not "did Sali learn
/// this once" but "can he do it *now*" — so availability leads, and under it sits every time he actually
/// reached for it, successes and failures alike. A capability with an empty log has never been exercised,
/// and that is stated rather than hidden.
private struct CapabilityDetailView: View {
    let capability: LearnedCapability

    @EnvironmentObject private var appState: AppState
    @State private var state: Loadable<CapabilityDetailResponse> = .idle

    var body: some View {
        content
            .navigationTitle(capability.displayName)
            .navigationBarTitleDisplayMode(.inline)
            .task { if case .idle = state { await load() } }
            .refreshable { await load() }
    }

    @ViewBuilder private var content: some View {
        switch state {
        case .idle, .loading:
            SaliSkeletonList(rows: 3, showsStats: true, showsBar: false)
        case .failed(let message):
            ErrorStateView(message) { Task { await load() } }
        case .loaded(let detail):
            List {
                Section {
                    VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                        Text(headline(for: detail))
                            .font(Theme.Typography.body)
                            .foregroundStyle(Theme.Colors.primaryText)
                            .fixedSize(horizontal: false, vertical: true)
                        if let confidence = capability.confidence {
                            ConfidenceReadout(value: confidence)
                        }
                    }
                    .padding(.vertical, Theme.Spacing.xs)
                } header: {
                    SectionHeader("Can he do this now?")
                } footer: {
                    SectionFooter(footnote(for: detail))
                }

                Section {
                    if detail.uses.isEmpty {
                        QuietNote("Never actually used. It was verified when it was learned, but nothing has reached for it since.")
                    } else {
                        ForEach(detail.uses) { CapabilityUseRow(use: $0) }
                    }
                } header: {
                    SectionHeader("When he used it",
                                  subtitle: "Every attempt the host logged, newest first.",
                                  emphasis: .secondary)
                }
            }
            .listStyle(.insetGrouped)
            .saliList()
        }
    }

    /// Plain language for the host's four availability states — no jargon leaks onto the screen.
    private func headline(for detail: CapabilityDetailResponse) -> String {
        switch detail.availability {
        case "available": "Yes — verified recently and working."
        case "stale": "Probably, but it hasn't been proven in a while. Sali re-checks before relying on it."
        case "degraded": "Not right now — it's been failing, so he stopped offering it."
        case "unavailable": "Not yet — it hasn't been verified here."
        default: (detail.usableNow ?? false) ? "Yes." : "Unknown — nothing has established this yet."
        }
    }

    private func footnote(for detail: CapabilityDetailResponse) -> String {
        let succeeded = capability.timesSucceeded ?? 0
        let failed = capability.timesFailed ?? 0
        guard succeeded + failed > 0 else { return "Confidence comes from evidence, not from claiming it." }
        return "\(succeeded) of \(succeeded + failed) attempts worked. Three failures in a row and Sali "
             + "marks it broken; a later success brings it back."
    }

    private func load() async {
        if case .loaded = state {} else { state = .loading }
        var query = ["name": capability.name ?? "", "scope": capability.scope ?? "environment"]
        if let reference = capability.scopeRef, !reference.isEmpty { query["scope_ref"] = reference }
        do {
            state = .loaded(try await appState.api.get("capabilities/detail", query: query))
        } catch {
            state = .failed("Couldn't load this capability just now.")
        }
    }
}

private struct CapabilityUseRow: View {
    let use: CapabilityUse

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                Text(use.action?.isEmpty == false ? use.action! : "used")
                    .font(Theme.Typography.body)
                    .foregroundStyle(Theme.Colors.primaryText)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: Theme.Spacing.s)
                if !use.worked { StatusPill("failed", color: Theme.Colors.warn) }
            }
            if let result = use.result, !result.isEmpty {
                Text(result)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            MetadataLine(items: metadata)
        }
        .padding(.vertical, Theme.Spacing.xs)
        .frame(minHeight: 44, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    private var metadata: [String] {
        var items: [String] = []
        if let usedAt = use.usedAt { items.append(usedAt.saliRelative) }
        if use.verified == true { items.append("independently checked") }
        return items
    }
}

private struct CapabilityRow: View {
    let capability: LearnedCapability

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                Text(capability.displayName)
                    .font(Theme.Typography.body.weight(.medium))
                    .foregroundStyle(Theme.Colors.primaryText)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: Theme.Spacing.s)
                if let status = capability.status, !status.isEmpty {
                    StatusPill(status.replacingUnderscores, color: statusInk)
                }
            }
            if let purpose = capability.purpose, !purpose.isEmpty {
                Text(purpose)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // Confidence and the facts under it each get their own line. Sharing one line left the
            // metadata about a third of the row wide, so every item truncated to "never u… · 1/1 wor…"
            // — three facts, none of them readable. Row height is cheaper than that.
            if let confidence = capability.confidence {
                ConfidenceReadout(value: confidence)
            }
            MetadataLine(items: metadata)
        }
        .padding(.vertical, Theme.Spacing.xs)
        .frame(minHeight: 44, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    /// Only what the host actually sent. `use_count` and `last_used` are the point of this row — a
    /// capability nobody reaches for is a claim, not an ability — so "never used" is stated rather than
    /// left blank.
    private var metadata: [String] {
        var items: [String] = []
        // "0 uses" and "never used" are the same sentence twice. A capability nothing has reached for
        // says it once; one that has been used says how often and when.
        let uses = capability.useCount ?? 0
        if uses == 0 {
            items.append("never used")
        } else {
            items.append(uses == 1 ? "1 use" : "\(uses) uses")
            if let lastUsed = capability.lastUsed { items.append(lastUsed.saliRelative) }
        }
        if let succeeded = capability.timesSucceeded, let failed = capability.timesFailed,
           succeeded + failed > 0 {
            items.append("\(succeeded)/\(succeeded + failed) worked")
        }
        if let place = capability.placeLabel { items.append("in \(place)") }
        return items
    }

    /// A working capability is the normal case and gets no tone for it; only a degraded or deprecated
    /// one is telling the user something.
    private var statusInk: Color {
        switch capability.status {
        case "verified", "available": Theme.Colors.primaryText
        case "degraded", "deprecated": Theme.Colors.warn
        default: Theme.Colors.secondaryText
        }
    }
}

private struct BuiltinGroupRow: View {
    let group: BuiltinCapabilityGroup

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                Text(group.displayName)
                    .font(Theme.Typography.body.weight(.medium))
                    .foregroundStyle(Theme.Colors.primaryText)
                Spacer(minLength: Theme.Spacing.s)
                Text("\(group.toolNames.count)")
                    .font(Theme.Typography.numeralSmall)
                    .foregroundStyle(Theme.Colors.tertiaryText)
            }
            if !group.toolNames.isEmpty {
                // Tool names are identifiers, so they are set as identifiers.
                Text(group.toolNames.joined(separator: "  ·  "))
                    .font(Theme.Typography.monoSmall)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .frame(minHeight: 44, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(group.displayName), \(group.toolNames.count) tools")
    }
}

private struct LessonRow: View {
    let candidate: LearningCandidateItem

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            Text(candidate.lesson ?? "A lesson Sali recorded")
                .font(Theme.Typography.body)
                .foregroundStyle(Theme.Colors.primaryText)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)

            HStack(alignment: .center, spacing: Theme.Spacing.m) {
                if let confidence = candidate.confidence {
                    ConfidenceReadout(value: confidence)
                }
                if let state = candidate.verificationState, !state.isEmpty {
                    StatusPill(state.replacingUnderscores, color: stateInk)
                }
                Spacer(minLength: Theme.Spacing.s)
                if let updated = candidate.updatedAt {
                    Text(updated.saliRelative)
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.tertiaryText)
                }
            }
            MetadataLine(items: metadata)
        }
        .padding(.vertical, Theme.Spacing.xs)
        .frame(minHeight: 44, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    /// The evidence behind the lesson — the part that makes it a lesson rather than an opinion.
    private var metadata: [String] {
        var items: [String] = []
        if let scope = candidate.scope, !scope.isEmpty { items.append("\(scope) scope") }
        if let level = candidate.evidenceLevel { items.append("evidence \(level)") }
        let worked = candidate.timesSuccessful ?? 0
        let didnt = candidate.timesFailed ?? 0
        if worked + didnt > 0 { items.append("\(worked) worked · \(didnt) didn't") }
        return items
    }

    /// `promoted` is the one state that means something changed; the rest are descriptive.
    private var stateInk: Color {
        candidate.verificationState == "promoted" ? Theme.Colors.primaryText
                                                  : Theme.Colors.secondaryText
    }
}

private struct ContradictionRow: View {
    let contradiction: LearningContradictionItem
    @ScaledMetric(relativeTo: .caption2) private var gutter: CGFloat = 34

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                Text((contradiction.claimKey ?? "A claim Sali holds").replacingUnderscores)
                    .font(Theme.Typography.body.weight(.medium))
                    .foregroundStyle(Theme.Colors.primaryText)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: Theme.Spacing.s)
                StatusPill((contradiction.status ?? "open").replacingUnderscores,
                           color: contradiction.isOpen ? Theme.Colors.warn : Theme.Colors.secondaryText)
            }
            // The two claims, set against each other, on the sunken ground reserved for quoted material.
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                claim("Was", contradiction.oldClaim)
                claim("Now", contradiction.newClaim)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(Theme.Spacing.m)
            .saliSurface(.sunken, radius: Theme.Radius.s)

            if let resolution = contradiction.resolution, !resolution.isEmpty {
                Text(resolution)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            MetadataLine(items: metadata)
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private func claim(_ label: String, _ text: String?) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
            Text(label)
                .font(Theme.Typography.metadata)
                .foregroundStyle(Theme.Colors.tertiaryText)
                .frame(width: gutter, alignment: .leading)
            Text(text?.isEmpty == false ? text! : "not recorded")
                .font(Theme.Typography.footnote)
                .foregroundStyle(text?.isEmpty == false ? Theme.Colors.primaryText
                                                        : Theme.Colors.tertiaryText)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var metadata: [String] {
        var items: [String] = []
        if let scope = contradiction.scope, !scope.isEmpty { items.append("\(scope) scope") }
        if let created = contradiction.createdAt { items.append(created.saliRelative) }
        return items
    }
}

/// A behavior change Sali would like to make, with the observation count that justifies it and the two
/// decisions that are the user's alone (§42). A refusal from the host is shown right here, on the row
/// the attempt was made from.
private struct ProposalRow: View {
    let proposal: BehaviorProposalItem
    let isWorking: Bool
    let canDecide: Bool
    let error: String?
    let onApprove: () -> Void
    let onReject: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.m) {
            VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                Text(proposal.proposedBehavior ?? "A behavior change Sali is proposing")
                    .font(Theme.Typography.body.weight(.medium))
                    .foregroundStyle(Theme.Colors.primaryText)
                    .fixedSize(horizontal: false, vertical: true)
                if let reason = proposal.reason, !reason.isEmpty {
                    Text(reason)
                        .font(Theme.Typography.footnote)
                        .foregroundStyle(Theme.Colors.secondaryText)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let trigger = proposal.trigger, !trigger.isEmpty {
                    HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                        Text("When")
                            .font(Theme.Typography.metadata)
                            .foregroundStyle(Theme.Colors.tertiaryText)
                        Text(trigger)
                            .font(Theme.Typography.footnote)
                            .foregroundStyle(Theme.Colors.primaryText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(Theme.Spacing.m)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .saliSurface(.sunken, radius: Theme.Radius.s)
                }
                HStack(spacing: Theme.Spacing.m) {
                    if let confidence = proposal.confidence {
                        ConfidenceReadout(value: confidence)
                    }
                    Spacer(minLength: Theme.Spacing.s)
                    MetadataLine(items: metadata)
                }
            }

            if isWorking {
                HStack(spacing: Theme.Spacing.s) {
                    ProgressView().controlSize(.small).tint(Theme.Colors.secondaryText)
                    Text("Telling Sali…")
                        .font(Theme.Typography.footnote)
                        .foregroundStyle(Theme.Colors.secondaryText)
                }
                .frame(maxWidth: .infinity, minHeight: 44)
            } else if canDecide {
                HStack(spacing: Theme.Spacing.m) {
                    Button(action: onReject) {
                        Text("Not now").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(SaliQuietButtonStyle())
                    .accessibilityLabel("Reject this behavior change")

                    Button(action: onApprove) {
                        Text("Approve").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(SaliPrimaryButtonStyle())
                    .accessibilityLabel("Approve this behavior change")
                }
            } else {
                Text("Only the owner or a controller can decide on this.")
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.tertiaryText)
            }

            if let error, !error.isEmpty {
                Text(error)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.danger)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, Theme.Spacing.s)
    }

    /// `times_observed` is the evidence the decision rests on, so it is said out loud.
    private var metadata: [String] {
        var items: [String] = []
        if let observed = proposal.timesObserved, observed > 0 {
            items.append(observed == 1 ? "seen once" : "seen \(observed) times")
        }
        if let scope = proposal.scope, !scope.isEmpty { items.append("\(scope) scope") }
        if let created = proposal.createdAt { items.append(created.saliRelative) }
        return items
    }
}

private extension String {
    var replacingUnderscores: String { replacingOccurrences(of: "_", with: " ") }
}
