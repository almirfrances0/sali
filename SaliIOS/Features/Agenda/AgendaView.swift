import SwiftUI

// MARK: - Wire types (feature-local; the endpoint contract is /api/v1/agenda)

/// One row from any of the eight sections. `id` is opaque (usually a UUID string), `when` is a
/// pre-rendered human phrase from the server's TemporalService (never an ISO timestamp), and
/// `detail` is absent from the JSON entirely when empty — Optional carries that correctly.
struct AgendaItem: Decodable, Equatable, Identifiable, Sendable {
    let kind: AgendaKind
    let id: String
    let title: String
    let when: String
    let reason: String
    let detail: AgendaDetail?
}

struct AgendaDetail: Decodable, Equatable, Sendable {
    let prompt: String?
    let timezone: String?
}

/// Forward-compatible kind: the server may add new kinds without a client bump; anything
/// unrecognised lands as `.unknown` and still renders (a generic glyph, no crash).
enum AgendaKind: String, Decodable, Sendable {
    case task, commitment, schedule, goal, initiative, unknown

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = AgendaKind(rawValue: raw) ?? .unknown
    }

    var icon: String {
        switch self {
        case .task:       "checkmark.circle"
        case .commitment: "hand.raised"
        case .schedule:   "clock"
        case .goal:       "target"
        case .initiative: "sparkle"
        case .unknown:    "circle.dotted"
        }
    }
}

/// Fixed-key response envelope. Every section is always present as an array; empty is a valid
/// steady state ("Clear agenda") — the view distinguishes that from failure explicitly.
struct AgendaResponse: Decodable, Equatable, Sendable {
    var now:         [AgendaItem] = []
    var today:       [AgendaItem] = []
    var overdue:     [AgendaItem] = []
    var waiting:     [AgendaItem] = []
    var blocked:     [AgendaItem] = []
    var upcoming:    [AgendaItem] = []
    var goals:       [AgendaItem] = []
    var initiatives: [AgendaItem] = []

    func items(for section: AgendaSection) -> [AgendaItem] {
        switch section {
        case .now:         now
        case .today:       today
        case .overdue:     overdue
        case .waiting:     waiting
        case .blocked:     blocked
        case .upcoming:    upcoming
        case .goals:       goals
        case .initiatives: initiatives
        }
    }

    var allSectionsAreEmpty: Bool {
        AgendaSection.allCases.allSatisfy { items(for: $0).isEmpty }
    }

    /// Used as the animation `value:` so SwiftUI cross-fades on rank changes but not on unchanged reloads.
    var contentHash: String {
        AgendaSection.allCases
            .map { "\($0.rawValue):\(items(for: $0).map(\.id).joined(separator: ","))" }
            .joined(separator: "|")
    }
}

// MARK: - Section identity (mirror of AgendaView.to_json order)

enum AgendaSection: String, CaseIterable {
    case now, today, overdue, waiting, blocked, upcoming, goals, initiatives

    var title: String {
        switch self {
        case .now: "Now"; case .today: "Today"; case .overdue: "Overdue"
        case .waiting: "Waiting"; case .blocked: "Blocked"; case .upcoming: "Upcoming"
        case .goals: "Goals"; case .initiatives: "Initiatives"
        }
    }

    var subtitle: String {
        switch self {
        case .now:         "In progress right now."
        case .today:       "Scheduled or due before the day ends."
        case .overdue:     "Past their deadline and unsettled."
        case .waiting:     "Owed a reply before Sali can move."
        case .blocked:     "Something is in the way."
        case .upcoming:    "Coming up in the days ahead."
        case .goals:       "Objectives that outlive a single task."
        case .initiatives: "Opportunities Sali noticed on its own."
        }
    }

    var isAttention: Bool { self == .overdue || self == .blocked }
    var pillLabel: String? { self == .overdue ? "Overdue" : self == .blocked ? "Blocked" : nil }

    var footerNote: String {
        switch self {
        case .now:         "The bar Sali actually pays attention to right now."
        case .today:       "Anything with a target time before the day ends."
        case .overdue:     "Sali surfaces these; it will not nag."
        case .waiting:     "Held until the other side moves."
        case .blocked:     "Sali will not retry until the block clears."
        case .upcoming:    "Not on today's list yet — planned ahead."
        case .goals:       "Sali picks tasks from these, not the other way around."
        case .initiatives: "Sali's own ideas — accept or dismiss any time."
        }
    }
}

// MARK: - Screen

/// The authoritative agenda: eight ordered bins the model and the phone both read from a single
/// synthesiser on the backend. Empty sections are silent — no hollow headers — mirroring the
/// server's `render()` semantics so the two surfaces cannot disagree about what is on the agenda.
struct AgendaView: View {
    @EnvironmentObject private var appState: AppState
    @StateObject private var viewModel = AgendaViewModel()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        Group {
            switch viewModel.state {
            case .idle, .loading:
                // Match the eventual row shape (title + reason + when) so content settles into
                // the placeholder instead of hard-swapping. Agenda items carry no progress bar.
                SaliSkeletonList(rows: 5, showsBar: false, lines: 2)
            case .failed(let message):
                ErrorStateView(message) { Task { await viewModel.load(api: appState.api) } }
            case .loaded(let response):
                if response.allSectionsAreEmpty {
                    EmptyStateView(
                        icon: "calendar",
                        title: "Clear agenda",
                        message: "Nothing pending. Anything Sali picks up shows up here."
                    )
                } else {
                    agendaList(response)
                }
            }
        }
        .background(Theme.Colors.background.ignoresSafeArea())
        .animation(
            Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle),
            value: viewModel.state.value?.contentHash
        )
        .navigationTitle("Agenda")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { PresenceToolbarItem() }
        .task {
            await viewModel.load(api: appState.api)
            viewModel.startAutoRefresh(api: appState.api)
        }
        .refreshable { await viewModel.load(api: appState.api) }
        .onDisappear { viewModel.stopAutoRefresh() }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { viewModel.startAutoRefresh(api: appState.api) }
            else                { viewModel.stopAutoRefresh() }
        }
        .onChange(of: appState.latestEvent?.id) { _, _ in
            if let event = appState.latestEvent {
                viewModel.handle(event: event, api: appState.api)
            }
        }
    }

    private func agendaList(_ response: AgendaResponse) -> some View {
        List {
            if let stale = viewModel.staleReason {
                AgendaStaleNotice(text: stale) { Task { await viewModel.load(api: appState.api) } }
            }
            ForEach(AgendaSection.allCases, id: \.rawValue) { section in
                let items = response.items(for: section)
                if !items.isEmpty {
                    Section {
                        // Section-scoped ForEach: the same commitment can legitimately appear in
                        // two sections (e.g. `now` + `today`); nesting keeps the identity space
                        // per section, so SwiftUI never sees a duplicate id at one level.
                        ForEach(items) { item in
                            AgendaRow(item: item, section: section)
                        }
                    } header: {
                        SectionHeader(section.title, subtitle: section.subtitle)
                    } footer: {
                        SectionFooter(
                            section.footerNote,
                            detail: "\(items.count) item\(items.count == 1 ? "" : "s")"
                        )
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
        .saliList()
    }
}

// MARK: - Row

private struct AgendaRow: View {
    let item: AgendaItem
    let section: AgendaSection

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.m) {
            // Ink glyph in every state — the kind of thing this row is (task/goal/promise/…),
            // not its severity. Attention lives in exactly one place per row: the trailing pill.
            Image(systemName: item.kind.icon)
                .font(Theme.Typography.body)
                .foregroundStyle(Theme.Colors.secondaryText)
                .frame(width: 22, alignment: .center)
                .accessibilityHidden(true)

            // supporting = the explaining sentence (why this landed here), metadata = the
            // temporal glance — matching the AgendaIntentRow contract at LifeView.swift:569-572.
            AgendaIntentRow(
                title:      item.title,
                supporting: item.reason.isEmpty ? nil : item.reason,
                metadata:   item.when.isEmpty ? nil : item.when,
                emphasis:   false
            ) {
                if let label = section.pillLabel {
                    StatusPill(label, color: Theme.Colors.warn)
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLabel)
    }

    private var accessibilityLabel: String {
        var parts: [String] = [item.kind.rawValue.capitalized, item.title]
        if !item.when.isEmpty   { parts.append(item.when) }
        if !item.reason.isEmpty { parts.append(item.reason) }
        if let pill = section.pillLabel { parts.append(pill) }
        return parts.joined(separator: ", ")
    }
}

// MARK: - Stale notice

private struct AgendaStaleNotice: View {
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
                .contentShape(Rectangle())
        }
        .padding(.vertical, Theme.Spacing.xs)
        .listRowBackground(Theme.Colors.surfaceSunken)
    }
}

#Preview {
    NavigationStack { AgendaView().environmentObject(AppState()) }
}
