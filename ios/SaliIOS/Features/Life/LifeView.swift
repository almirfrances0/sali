import SwiftUI

// MARK: - Forgiving local response shapes (§12/§20/§21)
//
// None of these have a shared Codable model yet (unlike PendingQuestion/ConsentRequest in DomainModels.swift),
// so — per the brief — they're decoded here with small, forgiving, file-local structs that mirror exactly
// what the backend stores actually SELECT (src/sali/tasks/{goals,initiatives,commitments,external}.py,
// src/sali/runtime/cognitive.py). Every field is optional except the ones the query guarantees, so an
// added/renamed backend column degrades a field to nil rather than breaking decoding.

/// `GET /api/v1/current-life` (alias `/cognitive`) → `CognitiveState.snapshot()`. Only the subset relevant
/// to the Life screen is modeled; `life_mode` is DERIVED server-side, never the model's say-so.
private struct CurrentLifeSnapshot: Decodable {
    let lifeMode: String?              // idle | working | waiting | interrupted
    let objective: String?
    let currentActivity: CurrentActivitySnapshot?
    let waitingReason: String?
    let blockedReason: String?
    let nextWakeup: Date?

    enum CodingKeys: String, CodingKey {
        case lifeMode = "life_mode", objective, currentActivity = "current_activity"
        case waitingReason = "waiting_reason", blockedReason = "blocked_reason"
        case nextWakeup = "next_wakeup"
    }
}

private struct CurrentActivitySnapshot: Decodable {
    let kind: String?
    let status: String?
    let description: String?
    let error: String?
}

/// `GoalStore.active()` (src/sali/tasks/goals.py) — goals that outlive a single task.
private struct LifeGoal: Decodable, Identifiable {
    let id: String
    let objective: String
    let origin: String?
    let status: String
    let priority: Int?
    let progress: Double?
    let deadline: Date?
    let nextAction: String?
    enum CodingKeys: String, CodingKey {
        case id, objective, origin, status, priority, progress, deadline
        case nextAction = "next_action"
    }
}

/// `InitiativeStore.open()` — opportunities Sali noticed on its own, deterministically scored. The query
/// doesn't select `reason_codes`/`evidence`, so `source` (e.g. "goal_progress", "routine") is the most
/// honest "why" available here — never a fabricated explanation.
private struct LifeInitiative: Decodable, Identifiable {
    let id: String
    let source: String?
    let title: String
    let status: String
    let priorityScore: Double?
    let attempts: Int?
    let createdAt: Date?
    enum CodingKeys: String, CodingKey {
        case id, source, title, status
        case priorityScore = "priority_score", attempts, createdAt = "created_at"
    }
}

/// `ObligationStore.open()` — Sali's unfinished business, never silently abandoned.
private struct LifeObligation: Decodable, Identifiable {
    let id: String
    let description: String
    let status: String
    let priority: Int?
    let blockingReason: String?
    let nextAction: String?
    let nextCheck: Date?
    enum CodingKeys: String, CodingKey {
        case id, description, status, priority
        case blockingReason = "blocking_reason", nextAction = "next_action", nextCheck = "next_check"
    }
}

/// `CommitmentStore.open()` — enduring responsibilities distinct from tasks.
private struct LifeCommitment: Decodable, Identifiable {
    let id: String
    let description: String
    let status: String
    let deadline: Date?
    let nextAction: String?
    enum CodingKeys: String, CodingKey {
        case id, description, status, deadline, nextAction = "next_action"
    }
}

/// `ExternalEntityStore.list_objects()` — accounts/sites/repos Sali owns or maintains. The query never
/// selects a secret/credential column, so there is nothing to accidentally leak here (§21) — only that the
/// object exists and its lifecycle state.
private struct DigitalObjectSummary: Decodable, Identifiable {
    let id: String
    let service: String?
    let ref: String?
    let objectType: String?
    let status: String?
    let verificationState: String?
    enum CodingKeys: String, CodingKey {
        case id, service, ref, objectType = "object_type", status
        case verificationState = "verification_state"
    }
    /// A generic display name — never a hardcoded provider name: whatever the object itself reports.
    var displayName: String { ref ?? service ?? (objectType?.capitalized ?? "Digital object") }
}

private struct GoalsResponse: Decodable { let active: [LifeGoal] }
private struct InitiativesResponse: Decodable { let open: [LifeInitiative] }
private struct ObligationsResponse: Decodable { let open: [LifeObligation] }
private struct CommitmentsResponse: Decodable { let open: [LifeCommitment] }
private struct DigitalObjectsResponse: Decodable { let objects: [DigitalObjectSummary] }
private struct PendingQuestionsResponse: Decodable { let waiting: [PendingQuestion] }
private struct ConsentResponse: Decodable { let pending: [ConsentRequest] }

/// `POST /api/v1/pending-questions/answer` response — routes a free-text reply to the right waiting
/// question, or reports ambiguity with candidates to disambiguate against (§35 natural consent: no by-ID
/// answer endpoint exists, so disambiguation works by resubmitting richer text, not by picking an ID).
private struct AnswerRoutingResult: Decodable {
    let status: String                  // answered | not_waiting | ambiguous | no_pending_question
    let candidates: [AmbiguousCandidate]?
    enum CodingKeys: String, CodingKey { case status, candidates }
}
private struct AmbiguousCandidate: Decodable, Identifiable, Hashable {
    let id: String
    let question: String
}
private struct ConsentRespondResult: Decodable {
    let status: String                  // granted | modified | declined | deferred | unclear
    let note: String?
}

// MARK: - View model

/// Drives `LifeView`: fans out across every autonomous-life endpoint concurrently (each independently
/// best-effort — one failing source never blanks the rest), and carries the natural free-text reply flow
/// for pending questions and consent (§35 — a reply, never a y/n gate).
@MainActor
final class LifeViewModel: ObservableObject {
    @Published private(set) var isLoading = false
    @Published private(set) var loadError: String?     // set only when EVERY source failed (e.g. offline)

    @Published private(set) var snapshot: CurrentLifeSnapshot?
    @Published private(set) var initiatives: [LifeInitiative] = []
    @Published private(set) var goals: [LifeGoal] = []
    @Published private(set) var obligations: [LifeObligation] = []
    @Published private(set) var commitments: [LifeCommitment] = []
    @Published private(set) var pendingQuestions: [PendingQuestion] = []
    @Published private(set) var consentRequests: [ConsentRequest] = []
    @Published private(set) var digitalObjects: [DigitalObjectSummary] = []

    @Published var questionReplyDrafts: [String: String] = [:]
    @Published var consentReplyDrafts: [String: String] = [:]
    @Published private(set) var ambiguousCandidates: [String: [AmbiguousCandidate]] = [:]
    @Published private(set) var actionNote: String?
    @Published private(set) var actionError: String?

    private var api: APIClient?
    private let pollInterval: UInt64 = 20_000_000_000   // 20s — autonomy can change without any push event

    func configure(api: APIClient) {
        if self.api == nil { self.api = api }
    }

    /// Loads once, then refreshes on a modest interval for as long as the caller's `.task` stays alive.
    func run() async {
        await loadAll()
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: pollInterval)
            if Task.isCancelled { break }
            await loadAll()
        }
    }

    func loadAll() async {
        guard let api else { return }
        isLoading = true
        defer { isLoading = false }

        async let snap: CurrentLifeSnapshot? = try? api.get("current-life")
        async let inits: InitiativesResponse? = try? api.get("initiatives")
        async let gls: GoalsResponse? = try? api.get("goals")
        async let obls: ObligationsResponse? = try? api.get("obligations")
        async let coms: CommitmentsResponse? = try? api.get("commitments")
        async let pqs: PendingQuestionsResponse? = try? api.get("pending-questions")
        async let cons: ConsentResponse? = try? api.get("consent")
        async let digs: DigitalObjectsResponse? = try? api.get("digital-objects")

        let (s, i, g, o, c, p, cr, d) = await (snap, inits, gls, obls, coms, pqs, cons, digs)

        snapshot = s
        initiatives = i?.open ?? []
        goals = g?.active ?? []
        obligations = o?.open ?? []
        commitments = c?.open ?? []
        pendingQuestions = p?.waiting ?? []
        consentRequests = cr?.pending ?? []
        digitalObjects = d?.objects ?? []

        // `try?` only yields nil on a genuine decode/network failure — an empty list still decodes fine.
        // So "everything nil" reliably means "couldn't reach Sali", not "Sali's life is quiet right now".
        loadError = (s == nil && i == nil && g == nil && o == nil && c == nil && p == nil && cr == nil && d == nil)
            ? "Can't reach Sali. Check your connection."
            : nil
    }

    func handleLiveEvent(_ event: SaliEvent) {
        switch event.type {
        case .taskWaiting, .agentMessage, .taskCompleted, .intentRevoked:
            Task { await loadAll() }
        default:
            break
        }
    }

    // MARK: - Natural free-text replies (§35 — the reply is the signal, never y/n)

    func submitQuestionAnswer(_ text: String, for question: PendingQuestion) async {
        guard let api else { return }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        let body: [String: Any] = ["person": question.personName as Any, "answer": trimmed]
            .compactMapValues { ($0 is NSNull) ? nil : $0 }
        do {
            let result: AnswerRoutingResult = try await api.post("pending-questions/answer", json: body)
            switch result.status {
            case "answered":
                ambiguousCandidates[question.id] = nil
                questionReplyDrafts[question.id] = nil
                actionNote = "Sent to Sali."
                await loadAll()
            case "ambiguous":
                ambiguousCandidates[question.id] = result.candidates ?? []
            case "no_pending_question", "not_waiting":
                actionError = "That question isn't waiting on you anymore."
            default:
                actionError = "Couldn't send that just now."
            }
        } catch {
            actionError = error.localizedDescription
        }
    }

    /// There's no by-ID answer endpoint (route_answer only matches by text), so picking a candidate nudges
    /// the reply toward it — richer, more specific text — rather than silently "selecting" it.
    func seedAmbiguousReply(for question: PendingQuestion, candidate: AmbiguousCandidate) {
        let existing = questionReplyDrafts[question.id] ?? ""
        let trimmedExisting = existing.trimmingCharacters(in: .whitespacesAndNewlines)
        questionReplyDrafts[question.id] = trimmedExisting.isEmpty
            ? "Re: \(candidate.question) — "
            : "Re: \(candidate.question) — \(trimmedExisting)"
    }

    func respondToConsent(_ text: String, for request: ConsentRequest) async {
        guard let api else { return }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        do {
            let result: ConsentRespondResult = try await api.post(
                "consent/\(request.id)/respond", json: ["response": trimmed])
            consentReplyDrafts[request.id] = nil
            actionNote = Self.noteFor(consentStatus: result.status, note: result.note)
            await loadAll()
        } catch {
            actionError = error.localizedDescription
        }
    }

    func clearActionFeedback() {
        actionNote = nil
        actionError = nil
    }

    private static func noteFor(consentStatus: String, note: String?) -> String {
        switch consentStatus {
        case "granted": return "Got it — go ahead."
        case "modified": return note.map { "Understood — narrowed to: \($0)" } ?? "Understood — narrowed scope."
        case "declined": return "Understood — Sali won't do that."
        case "deferred": return "Sali will ask again later."
        default: return "Sali will ask again, more specifically."
        }
    }
}

// MARK: - View

/// The autonomous Life interface (§12/§20/§21) — a tab root (see `RootView.swift`), so unlike `SystemView`
/// this owns its own `NavigationStack`. Autonomy stays visible & manageable: every section shows what Sali
/// noticed or is carrying, and pending questions/consent get a first-class free-text reply — never y/n
/// buttons (quick-reply chips are offered only as shortcuts into the same text field).
struct LifeView: View {
    @EnvironmentObject private var appState: AppState
    @StateObject private var viewModel = LifeViewModel()

    @State private var initiativesExpanded = true
    @State private var goalsExpanded = true
    @State private var commitmentsExpanded = true
    @State private var digitalExpanded = true

    var body: some View {
        NavigationStack {
            Group {
                if viewModel.isLoading && everythingEmpty {
                    LoadingState("Reading Sali's day…")
                } else if let loadError = viewModel.loadError, everythingEmpty {
                    ErrorStateView(loadError) { Task { await viewModel.loadAll() } }
                } else {
                    List {
                        currentLifeSection
                        waitingOnYouSection
                        initiativesSection
                        goalsSection
                        obligationsAndCommitmentsSection
                        digitalWorldSection
                    }
                    .listStyle(.insetGrouped)
                }
            }
            .navigationTitle("Life")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) { ConnectionBadge(appState.ws.state) }
            }
            .task {
                viewModel.configure(api: appState.api)
                await viewModel.run()
            }
            .refreshable { await viewModel.loadAll() }
            .onChange(of: appState.latestEvent?.id) { _, _ in
                if let event = appState.latestEvent { viewModel.handleLiveEvent(event) }
            }
            .alert("Sali", isPresented: actionAlertBinding) {
                Button("OK", role: .cancel) { viewModel.clearActionFeedback() }
            } message: {
                Text(viewModel.actionNote ?? viewModel.actionError ?? "")
            }
        }
    }

    private var everythingEmpty: Bool {
        viewModel.snapshot == nil && viewModel.initiatives.isEmpty && viewModel.goals.isEmpty &&
        viewModel.obligations.isEmpty && viewModel.commitments.isEmpty &&
        viewModel.pendingQuestions.isEmpty && viewModel.consentRequests.isEmpty && viewModel.digitalObjects.isEmpty
    }

    private var actionAlertBinding: Binding<Bool> {
        Binding(
            get: { viewModel.actionNote != nil || viewModel.actionError != nil },
            set: { isPresented in if !isPresented { viewModel.clearActionFeedback() } }
        )
    }

    // MARK: - Right now

    private var currentLifeSection: some View {
        Section {
            VStack(alignment: .leading, spacing: Theme.Spacing.m) {
                HStack {
                    Image(systemName: awakeIcon).foregroundStyle(awakeColor).accessibilityHidden(true)
                    Text(awakeTitle).font(Theme.Typography.heading)
                    Spacer()
                    if let mode = viewModel.snapshot?.lifeMode {
                        StatusPill(mode.capitalized, color: awakeColor)
                    }
                }

                if let activity = viewModel.snapshot?.currentActivity {
                    let text = (activity.description?.isEmpty == false ? activity.description : nil)
                        ?? activity.kind?.replacingOccurrences(of: "_", with: " ").capitalized ?? "Working"
                    Label(text, systemImage: "bolt.fill").font(Theme.Typography.body)
                } else if let objective = viewModel.snapshot?.objective, !objective.isEmpty {
                    Label(objective, systemImage: "target").font(Theme.Typography.body)
                } else {
                    Text("Nothing active right now.")
                        .font(Theme.Typography.body)
                        .foregroundStyle(Theme.Colors.secondaryText)
                }

                if let reason = viewModel.snapshot?.waitingReason, !reason.isEmpty {
                    Text("Waiting for your answer: \(reason)")
                        .font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.warn)
                } else if let reason = viewModel.snapshot?.blockedReason, !reason.isEmpty {
                    Text("Paused safely: \(reason)")
                        .font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.secondaryText)
                }

                if let wake = viewModel.snapshot?.nextWakeup {
                    Label("Next check-in \(wake.formatted(.relative(presentation: .named)))", systemImage: "clock")
                        .font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.secondaryText)
                }
            }
            .padding(.vertical, Theme.Spacing.xs)
        } header: {
            Text("Right now")
        }
    }

    private var awakeTitle: String {
        switch viewModel.snapshot?.lifeMode {
        case "working": "Active"
        case "waiting": "Waiting on you"
        case "interrupted": "Paused"
        case "idle": "Resting"
        default: "Unknown"
        }
    }
    private var awakeColor: Color {
        switch viewModel.snapshot?.lifeMode {
        case "working": Theme.Colors.accent
        case "waiting": Theme.Colors.warn
        case "interrupted": Theme.Colors.secondaryText
        case "idle": Theme.Colors.idle
        default: Theme.Colors.idle
        }
    }
    private var awakeIcon: String {
        switch viewModel.snapshot?.lifeMode {
        case "working": "bolt.fill"
        case "waiting": "hourglass"
        case "interrupted": "pause.circle.fill"
        case "idle": "moon.zzz.fill"
        default: "questionmark.circle.fill"
        }
    }

    // MARK: - Waiting on you (pending questions + consent)

    private var waitingOnYouSection: some View {
        Section {
            if viewModel.consentRequests.isEmpty && viewModel.pendingQuestions.isEmpty {
                Label("Nothing waiting on you right now.", systemImage: "checkmark.circle")
                    .font(Theme.Typography.body)
                    .foregroundStyle(Theme.Colors.secondaryText)
            } else {
                ForEach(viewModel.consentRequests) { request in
                    ConsentCard(
                        request: request,
                        draft: draftBinding(for: request),
                        canControl: appState.role.canControl,
                        onSend: { text in Task { await viewModel.respondToConsent(text, for: request) } }
                    )
                }
                ForEach(viewModel.pendingQuestions) { question in
                    QuestionCard(
                        question: question,
                        draft: draftBinding(for: question),
                        candidates: viewModel.ambiguousCandidates[question.id] ?? [],
                        canControl: appState.role.canControl,
                        onSend: { text in Task { await viewModel.submitQuestionAnswer(text, for: question) } },
                        onPickCandidate: { candidate in viewModel.seedAmbiguousReply(for: question, candidate: candidate) }
                    )
                }
            }
        } header: {
            Text("Waiting on you")
        } footer: {
            if !viewModel.consentRequests.isEmpty || !viewModel.pendingQuestions.isEmpty {
                Text("Reply in your own words — Sali reads free text, not just yes or no.")
            }
        }
    }

    private func draftBinding(for request: ConsentRequest) -> Binding<String> {
        Binding(get: { viewModel.consentReplyDrafts[request.id] ?? "" },
                set: { viewModel.consentReplyDrafts[request.id] = $0 })
    }
    private func draftBinding(for question: PendingQuestion) -> Binding<String> {
        Binding(get: { viewModel.questionReplyDrafts[question.id] ?? "" },
                set: { viewModel.questionReplyDrafts[question.id] = $0 })
    }

    // MARK: - Initiatives (autonomy, visible & manageable — §20)

    private var initiativesSection: some View {
        Section {
            DisclosureGroup(isExpanded: $initiativesExpanded) {
                if viewModel.initiatives.isEmpty {
                    Text("No open initiatives.").font(Theme.Typography.body).foregroundStyle(Theme.Colors.secondaryText)
                } else {
                    ForEach(viewModel.initiatives) { InitiativeRow(initiative: $0) }
                }
            } label: {
                Text("Sali noticed (\(viewModel.initiatives.count))").font(Theme.Typography.heading)
            }
        }
    }

    // MARK: - Goals

    private var goalsSection: some View {
        Section {
            DisclosureGroup(isExpanded: $goalsExpanded) {
                if viewModel.goals.isEmpty {
                    Text("No active goals.").font(Theme.Typography.body).foregroundStyle(Theme.Colors.secondaryText)
                } else {
                    ForEach(viewModel.goals) { GoalRow(goal: $0) }
                }
            } label: {
                Text("Goals (\(viewModel.goals.count))").font(Theme.Typography.heading)
            }
        }
    }

    // MARK: - Obligations & commitments

    private var obligationsAndCommitmentsSection: some View {
        Section {
            DisclosureGroup(isExpanded: $commitmentsExpanded) {
                if viewModel.obligations.isEmpty && viewModel.commitments.isEmpty {
                    Text("Nothing open.").font(Theme.Typography.body).foregroundStyle(Theme.Colors.secondaryText)
                } else {
                    ForEach(viewModel.obligations) { ObligationRow(obligation: $0) }
                    ForEach(viewModel.commitments) { CommitmentRow(commitment: $0) }
                }
            } label: {
                Text("Obligations & commitments (\(viewModel.obligations.count + viewModel.commitments.count))")
                    .font(Theme.Typography.heading)
            }
        }
    }

    // MARK: - Digital world (§21 — generic, no secrets, no hardcoded providers)

    private var digitalWorldSection: some View {
        Section {
            DisclosureGroup(isExpanded: $digitalExpanded) {
                if viewModel.digitalObjects.isEmpty {
                    Text("Sali doesn't own or maintain any digital objects yet.")
                        .font(Theme.Typography.body)
                        .foregroundStyle(Theme.Colors.secondaryText)
                } else {
                    ForEach(viewModel.digitalObjects) { DigitalObjectRow(object: $0) }
                }
            } label: {
                Text("Digital world (\(viewModel.digitalObjects.count))").font(Theme.Typography.heading)
            }
        } footer: {
            if !viewModel.digitalObjects.isEmpty {
                Text("Only lifecycle state is shown here — never credentials or secrets.")
            }
        }
    }
}

// MARK: - Cards & rows

private struct ConsentCard: View {
    let request: ConsentRequest
    @Binding var draft: String
    let canControl: Bool
    let onSend: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack(alignment: .top) {
                Image(systemName: "hand.raised.fill").foregroundStyle(Theme.Colors.warn).accessibilityHidden(true)
                Text("Sali needs your OK").font(Theme.Typography.heading)
                Spacer()
                if let level = request.judgmentLevel {
                    StatusPill(level.replacingOccurrences(of: "_", with: " ").capitalized, color: judgmentColor(level))
                }
            }
            Text(request.displayText).font(Theme.Typography.body).fixedSize(horizontal: false, vertical: true)
            if let consequence = request.consequence, !consequence.isEmpty {
                Text(consequence).font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
            }
            if let scope = request.scope, !scope.isEmpty {
                Label(scope, systemImage: "viewfinder").font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
            }
            if canControl {
                ReplyField(draft: $draft, placeholder: "Type your reply…", onSend: onSend)
                QuickReplyChips(chips: ["Yes, go ahead", "Not now", "Tell me more"], onSend: onSend)
            } else {
                Text("Only a controller or owner can respond.")
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }

    private func judgmentColor(_ level: String) -> Color {
        switch level {
        case "low", "normal": Theme.Colors.ok
        case "attention": Theme.Colors.info
        case "consent": Theme.Colors.warn
        case "high_consequence", "blocked": Theme.Colors.danger
        default: Theme.Colors.idle
        }
    }
}

private struct QuestionCard: View {
    let question: PendingQuestion
    @Binding var draft: String
    let candidates: [AmbiguousCandidate]
    let canControl: Bool
    let onSend: (String) -> Void
    let onPickCandidate: (AmbiguousCandidate) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack(alignment: .top) {
                Image(systemName: "questionmark.bubble.fill").foregroundStyle(Theme.Colors.info).accessibilityHidden(true)
                Text("Waiting for your answer").font(Theme.Typography.heading)
                Spacer()
                if let person = question.personName { StatusPill(person, color: Theme.Colors.idle) }
            }
            Text(question.question).font(Theme.Typography.body).fixedSize(horizontal: false, vertical: true)
            if let why = question.whyItMatters, !why.isEmpty {
                Text(why).font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
            }
            if !candidates.isEmpty {
                VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                    Text("Did you mean one of these? Tap to refine your reply:")
                        .font(Theme.Typography.caption.weight(.semibold))
                    ForEach(candidates) { candidate in
                        Button(candidate.question) { onPickCandidate(candidate) }
                            .font(Theme.Typography.caption)
                            .buttonStyle(.bordered)
                    }
                }
            }
            if canControl {
                ReplyField(draft: $draft, placeholder: "Type your answer…", onSend: onSend)
            } else {
                Text("Only a controller or owner can answer.")
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }
}

/// The shared free-text reply control (§35): a text field + Send. Never y/n buttons — this is the only way
/// to answer, quick-reply chips (where offered) are just shortcuts that fill this same field.
private struct ReplyField: View {
    @Binding var draft: String
    let placeholder: String
    let onSend: (String) -> Void

    var body: some View {
        HStack(alignment: .bottom, spacing: Theme.Spacing.s) {
            TextField(placeholder, text: $draft, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(1...4)
                .accessibilityLabel(placeholder)
            Button {
                let text = draft
                draft = ""
                onSend(text)
            } label: {
                Image(systemName: "paperplane.fill")
            }
            .buttonStyle(.borderedProminent)
            .tint(Theme.Colors.accent)
            .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            .accessibilityLabel("Send")
        }
    }
}

private struct QuickReplyChips: View {
    let chips: [String]
    let onSend: (String) -> Void
    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: Theme.Spacing.s) {
                ForEach(chips, id: \.self) { chip in
                    Button(chip) { onSend(chip) }
                        .font(Theme.Typography.caption)
                        .buttonStyle(.bordered)
                }
            }
        }
    }
}

private struct InitiativeRow: View {
    let initiative: LifeInitiative
    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            HStack(alignment: .top) {
                Text(initiative.title).font(Theme.Typography.body.weight(.medium)).lineLimit(2)
                Spacer()
                StatusPill(initiative.status.capitalized, color: statusColor(initiative.status))
            }
            if let source = initiative.source, !source.isEmpty {
                Label("Noticed via \(source.replacingOccurrences(of: "_", with: " "))", systemImage: "sparkles")
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }
    private func statusColor(_ status: String) -> Color {
        switch status {
        case "executing", "ready": Theme.Colors.accent
        case "blocked", "deferred": Theme.Colors.warn
        case "completed": Theme.Colors.ok
        default: Theme.Colors.idle
        }
    }
}

private struct GoalRow: View {
    let goal: LifeGoal
    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            HStack(alignment: .top) {
                Text(goal.objective).font(Theme.Typography.body.weight(.medium)).lineLimit(2)
                Spacer()
                StatusPill(goal.status.capitalized, color: statusColor(goal.status))
            }
            if let progress = goal.progress {
                ProgressView(value: max(0, min(1, progress)))
                    .tint(Theme.Colors.accent)
                    .accessibilityLabel("Progress \(Int(progress * 100))%")
            }
            if let next = goal.nextAction, !next.isEmpty {
                Text("Next: \(next)").font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
            }
            if let origin = goal.origin {
                Text("Origin: \(origin)").font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }
    private func statusColor(_ status: String) -> Color {
        switch status {
        case "active", "open": Theme.Colors.accent
        case "blocked": Theme.Colors.warn
        case "completed": Theme.Colors.ok
        default: Theme.Colors.idle
        }
    }
}

private struct ObligationRow: View {
    let obligation: LifeObligation
    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            HStack(alignment: .top) {
                Label(obligation.description, systemImage: "list.bullet.clipboard")
                    .font(Theme.Typography.body)
                    .lineLimit(2)
                Spacer()
                StatusPill(obligation.status.capitalized, color: obligation.status == "blocked" ? Theme.Colors.warn : Theme.Colors.idle)
            }
            if let reason = obligation.blockingReason, !reason.isEmpty {
                Text("Blocked: \(reason)").font(Theme.Typography.caption).foregroundStyle(Theme.Colors.warn)
            } else if let next = obligation.nextAction, !next.isEmpty {
                Text("Next: \(next)").font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }
}

private struct CommitmentRow: View {
    let commitment: LifeCommitment
    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            HStack(alignment: .top) {
                Label(commitment.description, systemImage: "hand.raised")
                    .font(Theme.Typography.body)
                    .lineLimit(2)
                Spacer()
                StatusPill(commitment.status.capitalized, color: Theme.Colors.idle)
            }
            if let deadline = commitment.deadline {
                Label(deadline.formatted(.relative(presentation: .named)), systemImage: "calendar")
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }
}

private struct DigitalObjectRow: View {
    let object: DigitalObjectSummary
    var body: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                Text(object.displayName).font(Theme.Typography.body.weight(.medium))
                if let type = object.objectType {
                    Text(type.replacingOccurrences(of: "_", with: " ").capitalized)
                        .font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.secondaryText)
                }
            }
            Spacer()
            if let status = object.status {
                StatusPill(status.capitalized, color: status == "active" ? Theme.Colors.ok : Theme.Colors.idle)
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }
}

#Preview {
    let appState = AppState()
    return LifeView()
        .environmentObject(appState)
}
