import SwiftUI

// MARK: - Sali's durable intent
//
// What this file used to be: a "Life" tab that opened with its own "Right now" card built from
// `GET /api/v1/current-life` — which is byte-for-byte `GET /api/v1/cognitive` (both routes are
// `return await runtime.snapshot()`), the same endpoint the Activity tab was already rendering under the
// same heading with a different subset of fields. Two adjacent tabs, one endpoint, neither complete.
//
// The duplication is gone and stays gone. "Right now" is defined once (`SaliSnapshot`) and rendered once
// (`RightNowCard`, on the Now tab); nothing in this file reads the snapshot or draws a live-state card.
// What survives here is the content that was genuinely unique and was buried *below* that duplication
// inside a four-way `DisclosureGroup`: Sali's durable intent — goals, initiatives, obligations,
// commitments — plus the digital objects it maintains.
//
// Where it went, and why: the ranked head of the agenda is folded into **Now**, directly below the live
// state, because durable intent is the same question as "what is Sali doing" at a longer time horizon —
// the objective on the Right Now card is usually descended from one of these goals, so separating them is
// what forced the mental merge in the first place. The full lists live here, reachable from Now and from
// the Sali hub. Only the digital-object inventory sits under Sali alone: it is a reference list of what
// Sali maintains, not a statement of intent, and it does not change hour to hour.
//
// MARK: - Forgiving response shapes (§12/§20/§21)
//
// Decoded with small, forgiving structs that mirror exactly what the backend stores actually SELECT
// (src/sali/tasks/{goals,initiatives,commitments,external}.py). Three layers of defence, because this
// screen has already been silently blanked once by a single field:
//
//  1. **Dates are Strings, parsed by `SaliISO8601`.** Python's `datetime.isoformat()` emits a
//     timezone-naive stamp for a naive datetime, which `ISO8601DateFormatter` rejects outright — and a
//     throwing date decode fails the WHOLE response. An unparseable stamp is now simply "no date".
//  2. **Everything the SQL doesn't guarantee is optional.** `status` included: it is a plain text column,
//     and a NULL there used to be fatal to the entire list.
//  3. **One bad row can't take the list with it** — `LenientList` decodes element by element and drops
//     only what it cannot read, reporting how many it dropped so the screen can say so out loud rather
//     than quietly showing a short list.

/// `GoalStore.active()` (src/sali/tasks/goals.py) — goals that outlive a single task. Returned best-first
/// by the store, so the order here is the backend's ranking, never a re-sort invented by the client.
struct LifeGoal: Decodable, Identifiable, Equatable, Sendable {
    let id: String
    let objective: String
    let origin: String?
    private let statusRaw: String?
    let priority: Int?
    let progress: Double?
    private let deadlineRaw: String?
    let nextAction: String?

    enum CodingKeys: String, CodingKey {
        case id, objective, origin, priority, progress
        case statusRaw = "status"
        case deadlineRaw = "deadline"
        case nextAction = "next_action"
    }

    var status: String { statusRaw ?? "active" }
    var deadline: Date? { SaliISO8601.parse(deadlineRaw) }
}

/// `InitiativeStore.open()` — opportunities Sali noticed on its own, deterministically scored. The query
/// doesn't select `reason_codes`/`evidence`, so `source` (e.g. "goal_progress", "routine") is the most
/// honest "why" available here — never a fabricated explanation.
struct LifeInitiative: Decodable, Identifiable, Equatable, Sendable {
    let id: String
    let source: String?
    let title: String
    private let statusRaw: String?
    let priorityScore: Double?
    let attempts: Int?
    private let createdAtRaw: String?

    enum CodingKeys: String, CodingKey {
        case id, source, title, attempts
        case statusRaw = "status"
        case priorityScore = "priority_score"
        case createdAtRaw = "created_at"
    }

    var status: String { statusRaw ?? "open" }
    var createdAt: Date? { SaliISO8601.parse(createdAtRaw) }
}

/// `ObligationStore.open()` — Sali's unfinished business, never silently abandoned.
struct LifeObligation: Decodable, Identifiable, Equatable, Sendable {
    let id: String
    let description: String
    private let statusRaw: String?
    let priority: Int?
    let blockingReason: String?
    let nextAction: String?
    private let nextCheckRaw: String?

    enum CodingKeys: String, CodingKey {
        case id, description, priority
        case statusRaw = "status"
        case blockingReason = "blocking_reason"
        case nextAction = "next_action"
        case nextCheckRaw = "next_check"
    }

    var status: String { statusRaw ?? "open" }
    var isBlocked: Bool { status == "blocked" || blockingReason?.isEmpty == false }
    var nextCheck: Date? { SaliISO8601.parse(nextCheckRaw) }
}

/// `CommitmentStore.open()` — enduring responsibilities distinct from tasks.
struct LifeCommitment: Decodable, Identifiable, Equatable, Sendable {
    let id: String
    let description: String
    private let statusRaw: String?
    private let deadlineRaw: String?
    let nextAction: String?

    enum CodingKeys: String, CodingKey {
        case id, description
        case statusRaw = "status"
        case deadlineRaw = "deadline"
        case nextAction = "next_action"
    }

    var status: String { statusRaw ?? "open" }
    var deadline: Date? { SaliISO8601.parse(deadlineRaw) }
}

/// `ExternalEntityStore.list_objects()` — accounts/sites/repos Sali owns or maintains. The query never
/// selects a secret/credential column, so there is nothing to accidentally leak here (§21) — only that the
/// object exists and its lifecycle state.
struct DigitalObjectSummary: Decodable, Identifiable, Equatable, Sendable {
    let id: String
    let service: String?
    let ref: String?
    let objectType: String?
    let status: String?
    let verificationState: String?
    private let lastVerifiedRaw: String?

    enum CodingKeys: String, CodingKey {
        case id, service, ref, status
        case objectType = "object_type"
        case verificationState = "verification_state"
        case lastVerifiedRaw = "last_verified"
    }

    /// A generic display name — never a hardcoded provider name: whatever the object itself reports.
    var displayName: String { ref ?? service ?? (objectType?.capitalized ?? "Digital object") }
    var lastVerified: Date? { SaliISO8601.parse(lastVerifiedRaw) }
}

// MARK: - Lenient collection decoding

/// Consumes exactly one element of an unkeyed container without ever throwing — the step-over used to get
/// past a row that failed to decode.
private struct SkippedElement: Decodable {
    init(from decoder: Decoder) throws {}
}

/// A list that survives its own bad rows.
///
/// `[T].init(from:)` is all-or-nothing: one malformed element fails the whole array, which fails the whole
/// response, which blanks the whole screen. This decodes element by element, keeps what it can read, and
/// counts what it dropped so the UI can be honest about a short list instead of silently pretending it is
/// complete.
///
/// The index check is load-bearing: `UnkeyedDecodingContainer.decode` is not specified to advance on a
/// throw (Foundation's implementations differ), so the loop verifies real progress and stops rather than
/// spinning forever on an element nothing can consume.
private struct LenientList<Element: Decodable & Sendable>: Decodable, Sendable {
    private(set) var items: [Element] = []
    private(set) var dropped: Int = 0

    init(from decoder: Decoder) throws {
        var container = try decoder.unkeyedContainer()
        while !container.isAtEnd {
            let index = container.currentIndex
            do {
                items.append(try container.decode(Element.self))
            } catch {
                dropped += 1
            }
            if container.currentIndex == index {
                guard (try? container.decode(SkippedElement.self)) != nil,
                      container.currentIndex > index else { break }
            }
        }
    }
}

/// The `counts` object every durable-intent endpoint returns alongside its list. All optional — it is
/// provenance for a footer, never something a row depends on.
private struct IntentCounts: Decodable, Equatable, Sendable {
    let active: Int?
    let open: Int?
    let completed: Int?
    let fulfilled: Int?
}

private struct GoalsResponse: Decodable, Sendable {
    let active: LenientList<LifeGoal>
    let counts: IntentCounts?
}
private struct InitiativesResponse: Decodable, Sendable {
    let open: LenientList<LifeInitiative>
    let counts: IntentCounts?
}
private struct ObligationsResponse: Decodable, Sendable {
    let open: LenientList<LifeObligation>
    let counts: IntentCounts?
}
private struct CommitmentsResponse: Decodable, Sendable {
    let open: LenientList<LifeCommitment>
    let counts: IntentCounts?
}
private struct DigitalObjectsResponse: Decodable, Sendable {
    let objects: LenientList<DigitalObjectSummary>
}

// MARK: - View model

/// Loads Sali's durable intent. Shared by the Agenda head on the Now tab and by the full `AgendaView`, so
/// the two can never rank the same items differently.
///
/// Event-driven, never polled. The old Life screen re-read eight endpoints every twenty seconds whether or
/// not anything had changed; durable intent by definition changes on an event, and those events are already
/// on the socket.
@MainActor
final class IntentAgendaViewModel: ObservableObject {
    @Published private(set) var state: Loadable<Void> = .idle
    @Published private(set) var goals: [LifeGoal] = []
    @Published private(set) var initiatives: [LifeInitiative] = []
    @Published private(set) var obligations: [LifeObligation] = []
    @Published private(set) var commitments: [LifeCommitment] = []

    @Published private(set) var completedGoals: Int?
    @Published private(set) var fulfilledCommitments: Int?

    /// What this screen could NOT read on the last pass. Four independent endpoints means a partial answer
    /// is normal, and a partial answer shown as if it were complete is the exact failure mode this screen
    /// was rebuilt to stop — so it is named rather than swallowed.
    @Published private(set) var unreadable: [String] = []
    /// Rows that decoded badly and were dropped. Counted, never hidden.
    @Published private(set) var droppedRows: Int = 0

    /// Everything open, as one number, for a summary line that never has to be recomputed by a caller.
    var openCount: Int { goals.count + initiatives.count + obligations.count + commitments.count }

    var isEmpty: Bool { openCount == 0 }

    var promiseCount: Int { obligations.count + commitments.count }

    func load(api: APIClient) async {
        if case .loaded = state {} else { state = .loading }

        async let goalsCall: GoalsResponse? = try? api.get("goals")
        async let initiativesCall: InitiativesResponse? = try? api.get("initiatives")
        async let obligationsCall: ObligationsResponse? = try? api.get("obligations")
        async let commitmentsCall: CommitmentsResponse? = try? api.get("commitments")

        let (g, i, o, c) = await (goalsCall, initiativesCall, obligationsCall, commitmentsCall)

        // A source that answered replaces its slice; a source that didn't keeps the last-known one, so a
        // flaky endpoint never empties a section that was populated a second ago.
        var missing: [String] = []
        var dropped = 0

        if let g { goals = g.active.items; dropped += g.active.dropped; completedGoals = g.counts?.completed }
        else { missing.append("goals") }

        if let i { initiatives = i.open.items; dropped += i.open.dropped }
        else { missing.append("initiatives") }

        if let o { obligations = o.open.items; dropped += o.open.dropped }
        else { missing.append("obligations") }

        if let c {
            commitments = c.open.items
            dropped += c.open.dropped
            fulfilledCommitments = c.counts?.fulfilled
        } else { missing.append("commitments") }

        unreadable = missing
        droppedRows = dropped

        // `try?` only yields nil on a genuine decode/network failure — an empty list still decodes fine.
        // So "every source nil" reliably means "couldn't reach Sali", not "Sali is carrying nothing".
        if missing.count == 4 {
            if case .loaded = state {} else { state = .failed("Can't reach Sali. Check your connection.") }
        } else {
            state = .loaded(())
        }
    }

    /// One sentence naming what is missing, or nil when the picture is complete.
    var incompleteNote: String? {
        var clauses: [String] = []
        if !unreadable.isEmpty {
            clauses.append("Sali didn't answer for \(Self.list(unreadable)) — showing the last reading.")
        }
        if droppedRows > 0 {
            clauses.append("\(droppedRows) item\(droppedRows == 1 ? "" : "s") couldn't be read and \(droppedRows == 1 ? "is" : "are") not shown.")
        }
        return clauses.isEmpty ? nil : clauses.joined(separator: " ")
    }

    private static func list(_ values: [String]) -> String {
        switch values.count {
        case 0: ""
        case 1: values[0]
        case 2: "\(values[0]) and \(values[1])"
        default: values.dropLast().joined(separator: ", ") + ", and " + (values.last ?? "")
        }
    }

    /// The events that actually change durable intent. Anything else on the socket is not this screen's
    /// business (§40).
    static let liveTriggers: Set<EventType> = [
        .taskCompleted, .intentRevoked,
        .other(.goalCreated), .other(.goalUpdated), .other(.goalCompleted), .other(.goalBlocked),
        .other(.initiativeCreated), .other(.initiativeSelected), .other(.initiativeCompleted),
        .other(.initiativeBlocked), .other(.initiativeDeferred),
        .other(.obligationCreated), .other(.obligationResolved), .other(.obligationReopened),
        .other(.commitmentCreated), .other(.commitmentFulfilled), .other(.commitmentCancelled),
        .other(.commitmentOverdue),
    ]
}

// MARK: - Agenda

/// The full durable-intent list. Reached from the ranked head on Now ("See the whole agenda") and from the
/// Sali hub. Not a tab: it is a destination, and a destination inherits the pushing stack's navigation.
///
/// Deliberately NOT on this screen: anything about what Sali is doing this second. That is the Now tab's
/// one job, and duplicating it here is what made both screens incomplete.
struct IntentAgendaView: View {
    @EnvironmentObject private var appState: AppState
    @StateObject private var viewModel = IntentAgendaViewModel()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Group {
            switch viewModel.state {
            case .idle, .loading:
                // Shaped like the rows underneath: a title, a trailing pill, a progress rule, a support line.
                SaliSkeletonList(rows: 5, showsPill: true, showsBar: true, lines: 1)
            case .failed(let message):
                ErrorStateView(message) { Task { await viewModel.load(api: appState.api) } }
            case .loaded:
                if viewModel.isEmpty {
                    EmptyStateView(
                        title: "Nothing on the agenda",
                        message: """
                        Sali isn't carrying any goals, initiatives, or promises right now. Anything it takes \
                        on — for you or on its own initiative — shows up here.
                        """
                    )
                } else {
                    agendaList
                }
            }
        }
        // The Theme canvas, not the system default. `saliList()` only paints behind a List — the empty,
        // error, and skeleton states are plain views, so without this they render on iOS's pure white and
        // the screen visibly changes colour the moment the content goes away.
        .background(Theme.Colors.background.ignoresSafeArea())
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle), value: viewModel.openCount)
        .navigationTitle("Agenda")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            PresenceToolbarItem()
        }
        .task { await viewModel.load(api: appState.api) }
        .refreshable { await viewModel.load(api: appState.api) }
        .onChange(of: appState.latestEvent?.id) { _, _ in
            guard let type = appState.latestEvent?.type,
                  IntentAgendaViewModel.liveTriggers.contains(type) else { return }
            Task { await viewModel.load(api: appState.api) }
        }
    }

    private var agendaList: some View {
        List {
            if let note = viewModel.incompleteNote {
                IncompleteNotice(text: note) { Task { await viewModel.load(api: appState.api) } }
            }

            if !viewModel.goals.isEmpty {
                Section {
                    ForEach(viewModel.goals) { GoalRow(goal: $0) }
                } header: {
                    SectionHeader("Goals",
                                  subtitle: "Objectives that outlive a single task, best first.")
                } footer: {
                    SectionFooter("Sali chooses what to work on from here, not from a to-do list.",
                                  detail: goalsDetail)
                }
            }

            if !viewModel.initiatives.isEmpty {
                Section {
                    ForEach(viewModel.initiatives) { InitiativeRow(initiative: $0) }
                } header: {
                    SectionHeader("Sali noticed",
                                  subtitle: "Opportunities Sali found on its own, deterministically scored.")
                } footer: {
                    SectionFooter("The score is arithmetic on evidence, not an opinion — nothing here started because Sali felt like it.",
                                  detail: "\(viewModel.initiatives.count) open")
                }
            }

            if viewModel.promiseCount > 0 {
                Section {
                    ForEach(viewModel.obligations) { ObligationRow(obligation: $0) }
                    ForEach(viewModel.commitments) { CommitmentRow(commitment: $0) }
                } header: {
                    SectionHeader("Promises",
                                  subtitle: "Unfinished business and enduring responsibilities.")
                } footer: {
                    SectionFooter("Nothing here is ever silently abandoned.", detail: promisesDetail)
                }
            }
        }
        .listStyle(.insetGrouped)
        .saliList()
    }

    private var goalsDetail: String {
        let open = "\(viewModel.goals.count) active"
        guard let completed = viewModel.completedGoals, completed > 0 else { return open }
        return "\(open) · \(completed) done"
    }

    private var promisesDetail: String {
        let open = "\(viewModel.promiseCount) open"
        guard let fulfilled = viewModel.fulfilledCommitments, fulfilled > 0 else { return open }
        return "\(open) · \(fulfilled) kept"
    }
}

// MARK: - Digital world (§21 — generic, no secrets, no hardcoded providers)

/// The accounts, sites, and repositories Sali owns or maintains. A reference inventory, which is why it
/// lives under the Sali hub rather than on Now: it states what exists, not what is happening.
struct DigitalWorldView: View {
    @EnvironmentObject private var appState: AppState
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var objects: [DigitalObjectSummary] = []
    @State private var droppedRows = 0
    @State private var state: Loadable<Void> = .idle
    @State private var refreshFailure: String?

    var body: some View {
        Group {
            switch state {
            case .idle, .loading:
                SaliSkeletonList(rows: 4, lines: 1)
            case .failed(let message):
                ErrorStateView(message) { Task { await load() } }
            case .loaded:
                if objects.isEmpty {
                    EmptyStateView(
                        icon: "globe",
                        title: "Nothing maintained yet",
                        message: """
                        Accounts, sites, and repositories Sali creates or takes responsibility for will be \
                        listed here — lifecycle state only, never credentials.
                        """
                    )
                } else {
                    objectList
                }
            }
        }
        // The Theme canvas, not the system default. `saliList()` only paints behind a List — the empty,
        // error, and skeleton states are plain views, so without this they render on iOS's pure white and
        // the screen visibly changes colour the moment the content goes away.
        .background(Theme.Colors.background.ignoresSafeArea())
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle), value: objects.count)
        .navigationTitle("Digital world")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            PresenceToolbarItem()
        }
        .task { await load() }
        .refreshable { await load() }
    }

    private var objectList: some View {
        List {
            if let note = incompleteNote {
                IncompleteNotice(text: note) { Task { await load() } }
            }
            Section {
                ForEach(objects) { DigitalObjectRow(object: $0) }
            } header: {
                SectionHeader("What Sali maintains",
                              subtitle: "Every account, site, and repository it is responsible for.")
            } footer: {
                SectionFooter("Only lifecycle state is shown here — never credentials or secrets.",
                              detail: "\(objects.count) tracked")
            }
        }
        .listStyle(.insetGrouped)
        .saliList()
    }

    private var incompleteNote: String? {
        var clauses: [String] = []
        if let refreshFailure { clauses.append("\(refreshFailure) Showing the last reading.") }
        if droppedRows > 0 {
            clauses.append("\(droppedRows) entr\(droppedRows == 1 ? "y" : "ies") couldn't be read and \(droppedRows == 1 ? "is" : "are") not shown.")
        }
        return clauses.isEmpty ? nil : clauses.joined(separator: " ")
    }

    private func load() async {
        if case .loaded = state {} else { state = .loading }
        do {
            let response: DigitalObjectsResponse = try await appState.api.get("digital-objects")
            objects = response.objects.items
            droppedRows = response.objects.dropped
            refreshFailure = nil
            state = .loaded(())
        } catch {
            let message = (error as? APIError)?.errorDescription
                ?? "Couldn't read what Sali maintains."
            // A failed refresh keeps the last-known inventory; only a cold failure takes the screen.
            if case .loaded = state { refreshFailure = message } else { state = .failed(message) }
        }
    }
}

// MARK: - Shared parts

/// The one "this picture is incomplete" row, used by both screens. It sits on `surfaceSunken` so it reads
/// as a note *about* the list rather than another item in it, and it always carries the retry — a partial
/// answer the person can't do anything about is just an apology.
private struct IncompleteNotice: View {
    let text: String
    let retry: () -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
            Image(systemName: "exclamationmark.circle")
                .font(Theme.Typography.caption)
                .foregroundStyle(Theme.Colors.tertiaryText)
            Text(text)
                .font(Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.secondaryText)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: Theme.Spacing.s)
            Button("Retry", action: retry)
                .font(Theme.Typography.footnote.weight(.semibold))
                .tint(Theme.Colors.accent)
                .frame(minHeight: 44)
        }
        .padding(.vertical, Theme.Spacing.xs)
        .listRowBackground(Theme.Colors.surfaceSunken)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Rows
//
// One row shape for all four kinds of durable intent: a title line that carries the weight, an optional
// status pill on the right baseline, one supporting sentence, and one line of machine metadata beneath it.
// Same leading edge, same vertical rhythm, so a mixed list reads as one column rather than four stacked
// designs — and the three ranks are always the same three ranks:
//
//   title       body/medium · primaryText     what Sali is trying to do
//   supporting  footnote     · secondaryText  the sentence that explains it
//   metadata    metadata     · tertiaryText   deadlines, scores, attempts — glanceable, never competing

/// The shared geometry every agenda row uses, so "Goals" and "Promises" line up down the same edge.
struct AgendaIntentRow<Trailing: View>: View {
    let title: String
    let supporting: String?
    let metadata: String?
    let emphasis: Bool
    @ViewBuilder var trailing: Trailing

    init(title: String, supporting: String? = nil, metadata: String? = nil, emphasis: Bool = false,
         @ViewBuilder trailing: () -> Trailing) {
        self.title = title
        self.supporting = supporting
        self.metadata = metadata
        self.emphasis = emphasis
        self.trailing = trailing()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                Text(title)
                    .font(Theme.Typography.body.weight(.medium))
                    .foregroundStyle(Theme.Colors.primaryText)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: Theme.Spacing.s)
                trailing
            }
            if let supporting, !supporting.isEmpty {
                Text(supporting)
                    // The one place colour is allowed in a row: genuine obstruction. Everything else stays
                    // ink, because a list where every second line is amber says nothing at all.
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(emphasis ? Theme.Colors.warn : Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let metadata, !metadata.isEmpty {
                Text(metadata)
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

/// A goal's progress, drawn from tokens rather than borrowed from `ProgressView` — the system bar's track
/// is a system grey that belongs to no step of the elevation ramp, which is exactly the sort of small
/// mismatch that makes a screen look assembled instead of designed.
private struct IntentProgressBar: View {
    let value: Double

    private var clamped: Double { max(0, min(1, value)) }

    var body: some View {
        HStack(spacing: Theme.Spacing.s) {
            GeometryReader { proxy in
                ZStack(alignment: .leading) {
                    Capsule().fill(Theme.Colors.placeholder)
                    Capsule().fill(Theme.Colors.accent)
                        .frame(width: max(3, proxy.size.width * clamped))
                }
            }
            .frame(height: 4)
            Text("\(Int((clamped * 100).rounded()))%")
                .font(Theme.Typography.numeralSmall)
                .foregroundStyle(Theme.Colors.tertiaryText)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Progress")
        .accessibilityValue("\(Int((clamped * 100).rounded())) percent")
    }
}

struct GoalRow: View {
    let goal: LifeGoal

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            AgendaIntentRow(title: goal.objective,
                            supporting: supporting,
                            metadata: metadata,
                            emphasis: goal.status == "blocked") {
                statusPill
            }
            if let progress = goal.progress, progress > 0 {
                IntentProgressBar(value: progress)
                    .padding(.bottom, Theme.Spacing.xs)
            }
        }
    }

    /// "Active" on every row is noise — the section header already said these are the active goals. Only a
    /// status that changes what the person should think earns a pill.
    @ViewBuilder private var statusPill: some View {
        if goal.status != "active" && goal.status != "open" {
            StatusPill(goal.status.capitalized,
                       color: goal.status == "blocked" ? Theme.Colors.warn : Theme.Colors.secondaryText)
        }
    }

    private var supporting: String? {
        if let next = goal.nextAction, !next.isEmpty { return "Next: \(next)" }
        if let origin = goal.origin, !origin.isEmpty { return "Origin: \(origin)" }
        return nil
    }

    /// The deadline used to be decoded and then never shown — the single most consequential fact about a
    /// goal was sitting unused in the model.
    private var metadata: String? {
        var parts: [String] = []
        if let deadline = goal.deadline {
            parts.append(deadline < Date() ? "Overdue \(deadline.saliRelative)" : "Due \(deadline.saliRelative)")
        }
        if goal.nextAction?.isEmpty == false, let origin = goal.origin, !origin.isEmpty {
            parts.append("from \(origin)")
        }
        return parts.isEmpty ? nil : parts.joined(separator: "  ·  ")
    }
}

struct InitiativeRow: View {
    let initiative: LifeInitiative

    var body: some View {
        AgendaIntentRow(title: initiative.title,
                        supporting: supporting,
                        metadata: metadata,
                        emphasis: initiative.status == "blocked") {
            if initiative.status != "open" {
                StatusPill(initiative.status.capitalized, color: statusColor)
            }
        }
    }

    private var supporting: String? {
        guard let source = initiative.source, !source.isEmpty else { return nil }
        return "Noticed via \(source.replacingOccurrences(of: "_", with: " "))"
    }

    /// The score and the attempt count are the honest account of *why this one and not another* — the
    /// ranking is arithmetic, so showing the arithmetic is the whole point.
    private var metadata: String? {
        var parts: [String] = []
        if let score = initiative.priorityScore {
            parts.append("score \(String(format: "%.2f", score))")
        }
        if let attempts = initiative.attempts, attempts > 0 {
            parts.append("tried \(attempts)×")
        }
        if let created = initiative.createdAt {
            parts.append("noticed \(created.saliRelative)")
        }
        return parts.isEmpty ? nil : parts.joined(separator: "  ·  ")
    }

    private var statusColor: Color {
        switch initiative.status {
        case "executing", "ready": Theme.Colors.secondaryText
        // Only genuine obstruction earns a tone; "deferred" is a decision, not a problem.
        case "blocked":            Theme.Colors.warn
        default:                   Theme.Colors.idle
        }
    }
}

struct ObligationRow: View {
    let obligation: LifeObligation

    var body: some View {
        AgendaIntentRow(title: obligation.description,
                        supporting: supporting,
                        metadata: metadata,
                        emphasis: obligation.isBlocked) {
            if obligation.status != "open" {
                StatusPill(obligation.status.replacingOccurrences(of: "_", with: " ").capitalized,
                           color: obligation.isBlocked ? Theme.Colors.warn : Theme.Colors.idle)
            }
        }
    }

    private var supporting: String? {
        if let reason = obligation.blockingReason, !reason.isEmpty { return "Blocked: \(reason)" }
        if let next = obligation.nextAction, !next.isEmpty { return "Next: \(next)" }
        return nil
    }

    /// `next_check` is Sali's own promise to come back to this. Decoded but never rendered before — which
    /// meant "never silently abandoned" was a claim the screen didn't actually show.
    private var metadata: String? {
        guard let nextCheck = obligation.nextCheck else { return nil }
        return nextCheck <= Date()
            ? "Due for another look now"
            : "Sali checks again \(nextCheck.saliRelative)"
    }
}

struct CommitmentRow: View {
    let commitment: LifeCommitment

    var body: some View {
        AgendaIntentRow(title: commitment.description,
                        supporting: supporting,
                        metadata: metadata,
                        emphasis: isOverdue) {
            if commitment.status != "open" {
                StatusPill(commitment.status.replacingOccurrences(of: "_", with: " ").capitalized,
                           color: Theme.Colors.idle)
            }
        }
    }

    private var isOverdue: Bool {
        guard let deadline = commitment.deadline else { return false }
        return deadline < Date()
    }

    private var supporting: String? {
        if let next = commitment.nextAction, !next.isEmpty { return "Next: \(next)" }
        if isOverdue { return "Past its deadline and still open." }
        return nil
    }

    /// One relative-date vocabulary across the whole app — this row used to speak
    /// `.formatted(.relative(presentation: .named))` while every neighbouring row said `saliRelative`.
    private var metadata: String? {
        guard let deadline = commitment.deadline else { return nil }
        return isOverdue ? "Was due \(deadline.saliRelative)" : "Due \(deadline.saliRelative)"
    }
}

struct DigitalObjectRow: View {
    let object: DigitalObjectSummary

    var body: some View {
        AgendaIntentRow(title: object.displayName,
                        supporting: supporting,
                        metadata: metadata) {
            if let status = object.status, status != "active" {
                StatusPill(status.replacingOccurrences(of: "_", with: " ").capitalized,
                           color: Theme.Colors.idle)
            }
        }
    }

    private var supporting: String? {
        var parts: [String] = []
        if let type = object.objectType, !type.isEmpty {
            parts.append(type.replacingOccurrences(of: "_", with: " ").capitalized)
        }
        // The provider is a fact the object reports about itself, never a hardcoded name — and it only
        // earns a line when it isn't already the title.
        if let service = object.service, !service.isEmpty, service != object.displayName {
            parts.append(service)
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// Freshness, which §16/§40 asks for everywhere: an object verified eight months ago is a different
    /// fact from the same object verified this morning.
    private var metadata: String? {
        var parts: [String] = []
        if let verification = object.verificationState, !verification.isEmpty {
            parts.append(verification.replacingOccurrences(of: "_", with: " "))
        }
        if let verified = object.lastVerified {
            parts.append("checked \(verified.saliRelative)")
        }
        return parts.isEmpty ? nil : parts.joined(separator: "  ·  ")
    }
}

#Preview {
    NavigationStack {
        IntentAgendaView().environmentObject(AppState())
    }
}
