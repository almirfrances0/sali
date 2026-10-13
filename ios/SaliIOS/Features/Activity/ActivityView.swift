import SwiftUI

// The **Now** tab (§13) — the one screen that answers "what is Sali doing, and what did it do?"
//
// It answers three questions, in this order, and the layout is nothing more than that order made visible:
//
//   1. WHAT IS SALI DOING RIGHT NOW  — `RightNowCard`, the hero. The only element on the screen allowed
//      to move, and the only one set in title type.
//   2. WHAT DOES HE NEED FROM ME     — `NeedsYouCard`, its own object directly beneath, never a paragraph
//      buried inside the hero. Present only when Sali is genuinely held on you.
//   3. WHAT HAS HE DONE TODAY        — the day-grouped feed, in footnote-to-body type on a hairline spine.
//
// Everything that answers none of those three recedes to a single line: Sali's durable intent used to take
// eight rows and four network calls in the middle of this screen, between the live state and today's
// history, answering a question nobody opened this tab to ask. It is now one tappable summary built from
// counts the shared snapshot already carries, and the full list is one tap away in Agenda.
//
// What it replaced before that: this screen and the Life tab each opened with a "Right now" card, fed by
// `GET /api/v1/cognitive` and `GET /api/v1/current-life` — two routes that are literally
// `return await runtime.snapshot()`, i.e. the same bytes. There is now one fetch (`AppState.snapshot`),
// one model (`SaliSnapshot`), and one card.
//
// The feed is seeded ONCE per refresh from the durable event log (`GET /api/v1/events`) and then left
// alone: everything after that arrives on the live socket (never a second polling loop, §40). Chain-of-
// thought is never surfaced — only `SaliEvent.humanSummary`, which is high-level by construction.

/// Pulls out a `Loadable`'s current value regardless of state — used across screens so a section can keep
/// showing last-known content while a background refresh is in flight or has failed transiently.
extension Loadable {
    var value: T? {
        if case .loaded(let value) = self { return value }
        return nil
    }
}

/// One row of `GET /api/v1/events` (src/sali/api/routes/api.py `get_events`) — the durable event log,
/// ordered by the monotonic `seq` (the event table's generated identity, NOT the random UUID). Decoded
/// forgivingly: a row without a type is skipped rather than failing the whole seed.
private struct DurableEventRow: Decodable {
    let seq: Int?
    let type: String?
    let payload: [String: JSONValue]?
    let timestamp: String?

    var event: SaliEvent? {
        guard let type, !type.isEmpty else { return nil }
        return SaliEvent.durable(rawType: type, sequence: seq, timestamp: timestamp, payload: payload ?? [:])
    }
}

// MARK: - Shared micro-typography

/// The one micro-label these two screens use: uppercase, tracked, metadata weight, tertiary ink.
///
/// It is what lets a field read as a *field* with no box, no rule and no colour around it — the single
/// cheapest way a monochrome layout buys hierarchy, and the reason the fact grid below can be dense
/// without turning into a wall.
struct MicroLabel: View {
    let text: String
    var ink: Color = Theme.Colors.tertiaryText

    init(_ text: String, ink: Color = Theme.Colors.tertiaryText) {
        self.text = text
        self.ink = ink
    }

    var body: some View {
        Text(text.uppercased())
            .font(Theme.Typography.metadata)
            .tracking(0.7)
            .foregroundStyle(ink)
    }
}

// MARK: - Ranking the feed

/// How loudly a row should speak. The feed used to render up to 200 rows as one undifferentiated texture,
/// so "what did Sali do today?" could only be answered by reading every line. Three ranks, decided once
/// here so every surface agrees, and expressed in type weight and ink — never in colour.
enum FeedRank: Int, Comparable, Sendable {
    /// Something changed in the world or in the work: a task began or ended, Sali reached out, a goal or
    /// promise moved, something failed. These are the answer to "what did Sali do today".
    case milestone = 2
    /// Real progress worth seeing, but not a landmark: a step, a resume, a completed piece of research.
    case notable = 1
    /// The texture between the landmarks — tool calls, streaming replies, housekeeping.
    case routine = 0

    static func < (lhs: FeedRank, rhs: FeedRank) -> Bool { lhs.rawValue < rhs.rawValue }

    static func of(_ event: SaliEvent) -> FeedRank {
        switch event.type {
        case .taskStarted, .taskCompleted, .taskWaiting, .intentRevoked,
             .agentMessage, .error, .resourceIncident:
            return .milestone
        case .taskProgress, .taskSuspended, .taskResumed, .researchCompleted:
            return .notable
        case .other(let family):
            switch family.group {
            case .goal, .initiative, .promise, .consent, .worldAction, .attention:
                return family.outcome == .neutral ? .notable : .milestone
            default:
                return family.outcome == .neutral ? .routine : .notable
            }
        default:
            return .routine
        }
    }
}

/// How much of the feed to show. Default is `.milestones`, because the honest default for a screen titled
/// "what did Sali do" is the landmarks — the texture is one tap away, never lost.
enum FeedScope: String, CaseIterable, Identifiable, Hashable {
    case milestones = "Milestones"
    case everything = "Everything"
    var id: String { rawValue }

    func admits(_ rank: FeedRank) -> Bool {
        switch self {
        case .milestones: rank >= .notable
        case .everything: true
        }
    }
}

/// One day of the feed. `date` is the start of that calendar day in the person's own time zone; events the
/// backend couldn't date land in a single trailing group rather than being silently attributed to today.
private struct FeedDay: Identifiable {
    let date: Date?
    let events: [SaliEvent]
    var id: Double { date?.timeIntervalSince1970 ?? -1 }
    var milestoneCount: Int { events.filter { FeedRank.of($0) == .milestone }.count }
}

// MARK: - View model

@MainActor
final class NowViewModel: ObservableObject {
    /// The durable history the live feed sits on top of. Loaded on appear and on pull-to-refresh — never on
    /// a timer, and never re-pulled for each incoming event (§40).
    @Published var history: Loadable<[SaliEvent]> = .idle
    @Published var scope: FeedScope = .milestones
    @Published var showsTechnical = false

    /// What belongs in "what Sali did" — families, so a new event type in an existing family shows
    /// up without an app release. Deliberately excludes the per-turn machinery (agent.status,
    /// self.presence, context.assembled, attention.*, execution.*): it is real, and it is not news.
    static let feedTypes = "task.,agent.message,goal.,commitment.,initiative.,learning.,memory.created,"
        + "grounding.,workspace.,tool.discovery_synced,schedule."

    /// Seeds the feed from the durable event log. This is a SEED, not a poll: it runs once per refresh and
    /// everything newer arrives on the socket, de-duplicated against these rows by sequence.
    func seedHistory(api: APIClient) async {
        let hadValue = history.value != nil
        if !hadValue { history = .loading }
        do {
            // ASK FOR MILESTONES, not the last 200 rows of anything. Measured on the live log,
            // an unfiltered request came back as 24 agent.status, 18 self.presence, 12
            // memory.created and turn housekeeping — with ZERO task, goal or Sali-initiated
            // messages in it. "What did Sali do today" rendered empty on a day with eight
            // conversations, because the interesting rows were pushed out by per-turn chatter.
            // A trailing "." means "this family", so the backend keeps its own event vocabulary.
            let rows: [DurableEventRow] = try await api.get(
                "events", query: ["limit": "200", "types": Self.feedTypes])
            history = .loaded(rows.compactMap(\.event))
        } catch {
            // Same posture as the snapshot: a transient failure keeps whatever we already have.
            if !hadValue {
                history = .failed((error as? APIError)?.errorDescription
                                  ?? "Couldn't read Sali's recent history.")
            }
        }
    }
}

// MARK: - Now

struct NowView: View {
    @EnvironmentObject private var appState: AppState
    @StateObject private var viewModel = NowViewModel()

    private static let maxFeedRows = 200

    var body: some View {
        NavigationStack {
            Group {
                if appState.snapshot.value == nil && viewModel.history.value == nil
                    && appState.liveEvents.isEmpty {
                    switch appState.snapshot {
                    case .failed(let message):
                        ErrorStateView(message) { Task { await loadAll() } }
                    default:
                        LoadingState("Reading what Sali is doing…")
                    }
                } else {
                    content
                }
            }
            .navigationTitle("Now")
            .toolbar {
                // No presence badge here on purpose: this screen states Sali's presence at hero scale
                // eight points below the nav bar, and saying it twice on one screen weakens both.
                ToolbarItem(placement: .primaryAction) {
                    Menu {
                        Toggle("Technical details", isOn: $viewModel.showsTechnical)
                    } label: {
                        // Standard bar metrics — no manual 44pt frame, no custom font. From iOS 26 the
                        // toolbar draws its own Liquid Glass sized to whatever the label reports, so both
                        // of those fought it: the 44×44 frame inflated the glass into a lopsided 57×41 blob
                        // beside the presence chip's clean capsule, and the smaller font shrank it to a
                        // 39pt circle. Left alone it is the same 45pt circle as the system back button.
                        // Measured on the simulator, the *touch* region is identical either way (the bar
                        // clips it), so the frame was buying nothing. Same root cause as the nested
                        // capsule (see `PresenceToolbarItem`): let the bar size its own chrome.
                        Image(systemName: "ellipsis")
                            .foregroundStyle(Theme.Colors.secondaryText)
                    }
                    .accessibilityLabel("Feed options")
                }
            }
        }
        .task { await loadAll() }
        .refreshable { await loadAll() }
    }

    private func loadAll() async {
        async let snapshot: Void = appState.refreshSnapshotAndWait()
        async let seed: Void = viewModel.seedHistory(api: appState.api)
        _ = await (snapshot, seed)
    }

    // MARK: Bands
    //
    // One rhythm rule for the whole screen: every block owns the gap ABOVE it and nothing else, so the
    // vertical intervals are decided in one place and read down the file in the order they appear.

    private var content: some View {
        List {
            heroBand
            attentionBand
            carryingBand
            feedBand
        }
        // `.plain` is what pins section headers on iOS, which is the whole point of the day spine: the day
        // you are reading stays named at the top of the screen while you scroll through it.
        .listStyle(.plain)
        .saliList()
        .environment(\.defaultMinListRowHeight, 0)
    }

    // 1 — What is Sali doing right now.
    private var heroBand: some View {
        Section {
            RightNowCard()
                .blockChrome(top: Theme.Spacing.m)
        }
    }

    // 2 — What does he need from me. Its own object, not a paragraph inside the hero: "Sali is blocked on
    // you" and "here is what Sali is doing" are two different facts and the person acts on only one.
    @ViewBuilder
    private var attentionBand: some View {
        if let snapshot = appState.snapshot.value, snapshot.isWaitingOnYou || snapshot.isBlocked {
            Section {
                NeedsYouCard(snapshot: snapshot) { appState.open(.chat) }
                    .blockChrome(top: Theme.Spacing.m)
            }
        }
    }

    // 2½ — Everything durable Sali is carrying, compressed to one line. It answers none of this screen's
    // three questions, so it gets one line and a way through, not a section.
    @ViewBuilder
    private var carryingBand: some View {
        if let summary = carryingSummary {
            Section {
                NavigationLink {
                    AgendaView()
                } label: {
                    HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.m) {
                        MicroLabel("Carrying")
                        Text(summary)
                            .font(Theme.Typography.footnote)
                            .foregroundStyle(Theme.Colors.secondaryText)
                            .lineLimit(1)
                        Spacer(minLength: 0)
                    }
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                }
                .accessibilityLabel("Carrying \(summary). Opens the full agenda.")
                .blockChrome(top: Theme.Spacing.blockGap)
            }
        }
    }

    /// Built ONLY from the one shared snapshot — the same numbers presence is derived from, so this line
    /// can never disagree with the card above it, and it costs no request of its own. Absent counts are
    /// absent, never zero-filled.
    private var carryingSummary: String? {
        guard let snapshot = appState.snapshot.value else { return nil }
        var parts: [String] = []
        if let goals = snapshot.activeGoals, goals > 0 {
            parts.append("\(goals) goal\(goals == 1 ? "" : "s")")
        }
        if let noticed = snapshot.openInitiatives, noticed > 0 {
            parts.append("\(noticed) noticed")
        }
        let promises = (snapshot.openObligations ?? 0) + (snapshot.commitments ?? 0)
        if promises > 0 {
            parts.append("\(promises) promise\(promises == 1 ? "" : "s")")
        }
        return parts.isEmpty ? nil : parts.joined(separator: "  ·  ")
    }

    // 3 — What has he done today.

    @ViewBuilder
    private var feedBand: some View {
        let days = feedDays

        Section {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.m) {
                Text("Activity")
                    .font(Theme.Typography.heading)
                    .foregroundStyle(Theme.Colors.primaryText)
                Spacer(minLength: Theme.Spacing.s)
                FeedScopeControl(scope: $viewModel.scope)
            }
            .accessibilityAddTraits(.isHeader)
            .blockChrome(top: Theme.Spacing.blockGap)
        }

        if days.isEmpty {
            Section {
                emptyFeed.blockChrome(top: Theme.Spacing.l)
            }
        } else {
            ForEach(days) { day in
                Section {
                    ForEach(day.events) { event in
                        FeedEventRow(event: event,
                                     rank: FeedRank.of(event),
                                     showsTechnical: viewModel.showsTechnical) {
                            appState.open(subjectOf: event.type, taskID: event.taskId)
                        }
                        .listRowInsets(EdgeInsets(top: 0, leading: Theme.Spacing.listMargin,
                                                  bottom: 0, trailing: Theme.Spacing.listMargin))
                        .listRowSeparator(.hidden)
                        .listRowBackground(Color.clear)
                    }
                } header: {
                    FeedDayHeader(date: day.date, detail: dayDetail(for: day))
                }
            }
        }
    }

    /// "3 of 12" when some of the day was landmarks, a plain total when none of it was. Both are counts of
    /// rows actually on screen under the current scope — never a promise about rows that are filtered out.
    private func dayDetail(for day: FeedDay) -> String {
        day.milestoneCount > 0 ? "\(day.milestoneCount) of \(day.events.count)" : "\(day.events.count)"
    }

    /// The feed: the durable history seeded from `GET /api/v1/events` with the live stream merged on top,
    /// de-duplicated, newest-first, filtered by scope, then grouped into days.
    private var feedDays: [FeedDay] {
        var merged: [String: SaliEvent] = [:]
        for event in (viewModel.history.value ?? []) where Self.isFeedWorthy(event) {
            merged[Self.identity(event)] = event
        }
        for event in appState.liveEvents where Self.isFeedWorthy(event) {
            merged[Self.identity(event)] = event   // a live frame always wins over its seeded copy
        }
        let ordered = merged.values
            .filter { viewModel.scope.admits(FeedRank.of($0)) }
            .sorted(by: Self.newerFirst)
            .prefix(Self.maxFeedRows)

        let calendar = Calendar.current
        var order: [Date?] = []
        var buckets: [Double: [SaliEvent]] = [:]
        for event in ordered {
            let day = event.timestamp.map { calendar.startOfDay(for: $0) }
            let key = day?.timeIntervalSince1970 ?? -1
            if buckets[key] == nil {
                buckets[key] = []
                order.append(day)
            }
            buckets[key, default: []].append(event)
        }
        return order.map { day in
            FeedDay(date: day, events: buckets[day?.timeIntervalSince1970 ?? -1] ?? [])
        }
    }

    /// Live and seeded events meet on `seq` — the durable log's monotonic key, the one identifier BOTH the
    /// socket frame and the REST row always carry. The event id is the fallback for a live frame with no
    /// sequence yet.
    private static func identity(_ event: SaliEvent) -> String {
        event.sequence.map { "seq:\($0)" } ?? "id:\(event.id)"
    }

    private static func newerFirst(_ a: SaliEvent, _ b: SaliEvent) -> Bool {
        if let x = a.sequence, let y = b.sequence, x != y { return x > y }
        let ta = a.timestamp ?? .distantPast
        let tb = b.timestamp ?? .distantPast
        if ta != tb { return ta > tb }
        return a.id > b.id   // a total order, so rows never shuffle between renders
    }

    /// Events a person would actually recognize as "activity" — connection housekeeping and raw streaming
    /// deltas are noise here, not signal, and so is a real backend event this build has no screen for
    /// (`.other(.unknown)`): its raw name would be machine text, not activity.
    private static func isFeedWorthy(_ event: SaliEvent) -> Bool {
        event.type != .messageDelta && !event.type.isBackgroundNoise
    }

    /// An empty feed means one of three different things, and saying "Nothing yet" for all of them is how
    /// this screen used to lie about a backend that had plenty of history.
    @ViewBuilder
    private var emptyFeed: some View {
        switch viewModel.history {
        case .idle, .loading:
            VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                SaliSkeleton(height: 13)
                SaliSkeleton(height: 13).padding(.trailing, 96)
            }
        case .failed(let message):
            VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                Text(message)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
                Button("Try again") { Task { await viewModel.seedHistory(api: appState.api) } }
                    .font(Theme.Typography.footnote.weight(.medium))
                    .tint(Theme.Colors.primaryText)
                    .frame(minHeight: 44)
            }
        case .loaded:
            Text(viewModel.scope == .milestones
                 ? "No milestones yet. Switch to Everything to see the routine steps."
                 : "Nothing yet — Sali's activity will appear here as it happens.")
                .font(Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.secondaryText)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

extension View {
    /// One chrome for every full-width block on the Now and Inbox canvases: the screen's leading edge, no
    /// row furniture, and the gap ABOVE it. Blocks never carry bottom space, so two adjacent gaps can
    /// never compound into an interval nobody chose.
    func blockChrome(top: CGFloat) -> some View {
        self
            .listRowInsets(EdgeInsets(top: top, leading: Theme.Spacing.listMargin,
                                      bottom: 0, trailing: Theme.Spacing.listMargin))
            .listRowSeparator(.hidden)
            .listRowBackground(Color.clear)
    }
}

// MARK: - Right now

/// The ONE "Right now", rendered from the ONE snapshot (`AppState.snapshot`, `GET /api/v1/cognitive`), as
/// the hero of the screen.
///
/// The card is built as a masthead: a presence line, a hairline rule, then the headline in title type. That
/// order is the point — before you read a word of what Sali is doing, the glyph and the tracked label have
/// already told you *whether* it is doing anything, and the rule underneath is what makes the two read as
/// a stated fact rather than as a label floating above a paragraph.
///
/// Everything else in the card is deliberately quieter than the headline: the next action is a labelled
/// line, the operational facts are a two-column metadata grid behind a divider, and provenance appears
/// only when the card is NOT live. Every field degrades to absent rather than to a guess.
struct RightNowCard: View {
    @EnvironmentObject private var appState: AppState
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .subheadline) private var heroGlyph: CGFloat = 18

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.l) {
            masthead
            // Last-known content always wins over a spinner (§16): only a state that has never loaded is
            // allowed to show a skeleton or an error in this card's place.
            if let snapshot = appState.snapshot.value {
                detail(for: snapshot)
            } else {
                switch appState.snapshot {
                case .failed(let message):
                    VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                        Text(message)
                            .font(Theme.Typography.footnote)
                            .foregroundStyle(Theme.Colors.secondaryText)
                            .fixedSize(horizontal: false, vertical: true)
                        Button("Try again") { appState.refreshSnapshot(force: true) }
                            .font(Theme.Typography.footnote.weight(.medium))
                            .tint(Theme.Colors.primaryText)
                            .frame(minHeight: 44)
                    }
                default:
                    SaliSkeletonRow(lines: 2, showsPill: false)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Theme.Spacing.xl)
        .background(Theme.Colors.surfaceRaised)
        .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.l, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.l, style: .continuous)
                // The card's own edge thickens while Sali is working — the same static "alive" signal the
                // presence chip uses, at card scale, so the hero reads as live in a still screenshot too.
                .strokeBorder(appState.presence.isBusy ? Theme.Colors.borderStrong : Theme.Colors.border,
                              lineWidth: appState.presence.isBusy ? Theme.Stroke.emphasis
                                                                  : Theme.Stroke.hairline)
        )
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle), value: appState.presence)
    }

    // MARK: Masthead — presence, stated once, at the top of the screen's hierarchy

    private var masthead: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.m) {
            HStack(alignment: .center, spacing: Theme.Spacing.s) {
                SaliStateGlyph(appState.presence.glyph,
                               animated: !reduceMotion,
                               side: heroGlyph,
                               ink: mastheadInk)
                Text(appState.presence.label.uppercased())
                    .font(Theme.Typography.subheading)
                    .tracking(1.0)
                    .foregroundStyle(mastheadInk)
                Spacer(minLength: Theme.Spacing.s)
                provenance
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Sali: \(appState.presence.label)")
            .accessibilityValue(appState.presence.explanation)

            Rectangle()
                .fill(Theme.Colors.separator)
                .frame(height: Theme.Stroke.hairline)
                .accessibilityHidden(true)
        }
    }

    /// Full ink whenever Sali is under its own power or holding for you; quieter when it is merely present;
    /// quietest when we cannot see it at all. Three tiers of one ink, no hue.
    private var mastheadInk: Color {
        switch appState.presence {
        case .offline:                          Theme.Colors.tertiaryText
        case .idle, .blocked:                   Theme.Colors.secondaryText
        case .thinking, .working, .waitingOnYou: Theme.Colors.primaryText
        }
    }

    /// Provenance, shown only when the card is NOT live. A stale snapshot rendered without a timestamp is
    /// the app claiming to know something it doesn't.
    @ViewBuilder
    private var provenance: some View {
        if appState.presence == .offline, let updated = appState.snapshotUpdatedAt {
            Text("Last read \(updated.saliRelative)")
                .font(Theme.Typography.metadata)
                .foregroundStyle(Theme.Colors.tertiaryText)
        }
    }

    @ViewBuilder
    private func detail(for snapshot: SaliSnapshot) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.l) {
            headline(for: snapshot)

            if let next = snapshot.nextAction, !next.isEmpty {
                LabelledLine(label: "Next", value: next)
            }

            facts(for: snapshot)

            if snapshot.taskID != nil {
                Button {
                    appState.openTask(snapshot.taskID)
                } label: {
                    HStack(spacing: Theme.Spacing.xs) {
                        Text("Open in Work")
                        Image(systemName: "arrow.up.right")
                            .font(Theme.Typography.metadata)
                    }
                    .font(Theme.Typography.callout.weight(.medium))
                    .foregroundStyle(Theme.Colors.primaryText)
                    .frame(minHeight: 44, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
    }

    @ViewBuilder
    private func headline(for snapshot: SaliSnapshot) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            if let headline = snapshot.headline {
                Text(headline)
                    .font(Theme.Typography.title)
                    .foregroundStyle(Theme.Colors.primaryText)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text(appState.presence == .offline
                     ? "Not connected — nothing here is live."
                     : "Nothing in progress right now.")
                    .font(Theme.Typography.titleSmall)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let context = snapshot.headlineContext {
                Text(context)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// The operational facts, on one grid so labels and values share exactly two columns and one baseline
    /// rhythm. Set in tracked micro-caps against footnote values, it reads as a spec block under the
    /// headline rather than as more sentences competing with it.
    @ViewBuilder
    private func facts(for snapshot: SaliSnapshot) -> some View {
        let rows = Self.facts(from: snapshot, alreadyStated: appState.presence.label)
        if !rows.isEmpty {
            VStack(alignment: .leading, spacing: 0) {
                Rectangle()
                    .fill(Theme.Colors.separator)
                    .frame(height: Theme.Stroke.hairline)
                    .padding(.bottom, Theme.Spacing.m)
                Grid(alignment: .leadingFirstTextBaseline,
                     horizontalSpacing: Theme.Spacing.m,
                     verticalSpacing: Theme.Spacing.s) {
                    ForEach(rows, id: \.label) { row in
                        GridRow {
                            MicroLabel(row.label)
                                .gridColumnAlignment(.leading)
                            Text(row.value)
                                .font(row.monospaced ? Theme.Typography.monoSmall
                                                     : Theme.Typography.footnote)
                                .foregroundStyle(Theme.Colors.secondaryText)
                                .fixedSize(horizontal: false, vertical: true)
                                .gridColumnAlignment(.leading)
                        }
                        .accessibilityElement(children: .combine)
                        .accessibilityLabel("\(row.label): \(row.value)")
                    }
                }
            }
        }
    }

    struct Fact { let label: String; let value: String; var monospaced = false }

    /// Every fact both old cards had between them, in one order, each omitted when the backend has nothing.
    /// - Parameter alreadyStated: the label the masthead is already showing. A fact that repeats it is
    ///   dropped: the card led with "IDLE" in tracked caps and then spent a whole metadata row on
    ///   "Life mode — Idle", which is the same word said twice and reads as a layout that isn't paying
    ///   attention. The row still appears the moment life mode says something the masthead does not.
    static func facts(from snapshot: SaliSnapshot, alreadyStated: String? = nil) -> [Fact] {
        var rows: [Fact] = []
        if let status = snapshot.taskStatus, !status.isEmpty {
            rows.append(Fact(label: "Task", value: (TaskState(rawValue: status) ?? .other).label))
        }
        if let phase = snapshot.phase, !phase.isEmpty {
            rows.append(Fact(label: "Phase", value: phase.replacingOccurrences(of: "_", with: " ")
                .capitalized))
        }
        if let step = snapshot.currentStep {
            rows.append(Fact(label: "Step", value: "\(step)"))
        }
        if let mode = snapshot.lifeMode, !mode.isEmpty,
           mode.caseInsensitiveCompare(alreadyStated ?? "") != .orderedSame {
            rows.append(Fact(label: "Life mode", value: mode.capitalized))
        }
        if let tool = snapshot.activeTool, !tool.isEmpty {
            rows.append(Fact(label: "Tool", value: tool, monospaced: true))
        }
        if let workspace = snapshot.workspace, !workspace.isEmpty {
            rows.append(Fact(label: "Workspace",
                             value: URL(fileURLWithPath: workspace).lastPathComponent,
                             monospaced: true))
        }
        if let count = snapshot.researchCount, count > 0 {
            rows.append(Fact(label: "Research", value: "\(count) source\(count == 1 ? "" : "s")"))
        }
        if let review = snapshot.reviewStatus, !review.isEmpty {
            rows.append(Fact(label: "Review", value: review.capitalized))
        }
        if let wake = snapshot.nextWakeup {
            // `saliRelative`, not `.formatted(.relative:)`, so an unreadable stamp says so instead of
            // announcing a confident "56 years ago" — and so this row is phrased like every neighbour.
            rows.append(Fact(label: "Next wake", value: wake.saliRelative))
        }
        return rows
    }
}

// MARK: - Needs you

/// The second question the screen answers, as its own object.
///
/// It used to be a grey inset paragraph inside the hero card, which put "Sali is blocked until you answer"
/// at the same rank as "Sali's workspace is /srv/x". It is now a peer of the hero with a heavier edge, a
/// full-ink micro-label, and one unmistakable action — and it is the ONLY card in the app that carries a
/// filled ink button, because it is the only place where the person is the blocking dependency.
///
/// Emphasis is entirely value and weight: `Stroke.emphasis` on `borderStrong`, primary ink on the label,
/// an ink-filled action. Nothing here is a hue, so it escalates identically in light and dark.
private struct NeedsYouCard: View {
    let snapshot: SaliSnapshot
    let onAnswer: () -> Void

    /// Waiting outranks blocked: if Sali is both, the actionable one is the one to show.
    private var isAsk: Bool { snapshot.isWaitingOnYou }

    /// Sali's own words where it has them; otherwise the plain, derived truth. Never a placeholder.
    private var reason: String {
        if let reason = snapshot.attentionReason, !reason.isEmpty { return reason }
        return isAsk ? "Sali is holding this until you answer."
                     : "Sali stopped itself rather than guess."
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.l) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                MicroLabel(isAsk ? "Needs you" : "Paused safely", ink: Theme.Colors.primaryText)
                Spacer(minLength: Theme.Spacing.s)
                if let objective = snapshot.objective, !objective.isEmpty {
                    Text(objective)
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.tertiaryText)
                        .lineLimit(1)
                }
            }

            Text(reason)
                .font(Theme.Typography.body)
                .foregroundStyle(Theme.Colors.primaryText)
                .fixedSize(horizontal: false, vertical: true)

            if isAsk {
                Button(action: onAnswer) {
                    HStack(spacing: Theme.Spacing.xs) {
                        Text("Answer in chat")
                        Image(systemName: "arrow.up.right").font(Theme.Typography.metadata)
                    }
                    .frame(maxWidth: .infinity, minHeight: 22)
                }
                .buttonStyle(SaliPrimaryButtonStyle())
                .accessibilityHint("Opens the chat, where you can reply in your own words")
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Theme.Spacing.xl)
        .background(Theme.Colors.surfaceRaised)
        .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.l, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.l, style: .continuous)
                .strokeBorder(Theme.Colors.borderStrong, lineWidth: Theme.Stroke.emphasis)
        )
        .accessibilityElement(children: .contain)
    }
}

/// A single labelled line, sharing the card's leading edge. Used where a fact needs a full-width sentence
/// rather than a grid cell.
private struct LabelledLine: View {
    let label: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            MicroLabel(label)
            Text(value)
                .font(Theme.Typography.callout)
                .foregroundStyle(Theme.Colors.secondaryText)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(label): \(value)")
    }
}

// MARK: - Scope control

/// Two words and a hairline, instead of the stock segmented control that used to run the full width of the
/// screen above the feed.
///
/// A segmented control is a *decision* — it asks for the same visual weight as the content it filters, and
/// at 32pt tall and 350pt wide it was the loudest object on a screen whose whole job is a live state. This
/// is the same choice, made in the header line it belongs to: the active scope is ink and semibold, the
/// other is tertiary and regular, and each still owns a 44pt target.
private struct FeedScopeControl: View {
    @Binding var scope: FeedScope
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        HStack(spacing: Theme.Spacing.s) {
            ForEach(Array(FeedScope.allCases.enumerated()), id: \.element) { index, option in
                if index > 0 {
                    Rectangle()
                        .fill(Theme.Colors.border)
                        .frame(width: Theme.Stroke.hairline, height: 11)
                        .accessibilityHidden(true)
                }
                Button {
                    scope = option
                } label: {
                    Text(option.rawValue.uppercased())
                        .font(Theme.Typography.metadata)
                        .tracking(0.7)
                        .foregroundStyle(scope == option ? Theme.Colors.primaryText
                                                         : Theme.Colors.tertiaryText)
                        .fontWeight(scope == option ? .semibold : .regular)
                        .frame(minHeight: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(option.rawValue)
                .accessibilityAddTraits(scope == option ? [.isButton, .isSelected] : .isButton)
            }
        }
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick), value: scope)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("How much of the feed to show")
    }
}

// MARK: - The day spine

/// The sticky header that gives a chronological list its spine — used by BOTH the Now feed and the Inbox
/// message list, so the two screens share one calendar language instead of inventing two.
///
/// Names the day in the person's own calendar and says how much is in it, so scrolling answers "what
/// happened on Tuesday" without reading a single row.
struct FeedDayHeader: View {
    let date: Date?
    var detail: String?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
            Text(SaliDay.title(for: date).uppercased())
                .font(Theme.Typography.metadata)
                .tracking(1.0)
                .foregroundStyle(Theme.Colors.primaryText)
            Spacer(minLength: Theme.Spacing.s)
            if let detail {
                Text(detail)
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .monospacedDigit()
            }
        }
        .padding(.horizontal, Theme.Spacing.listMargin)
        .padding(.top, Theme.Spacing.l)
        .padding(.bottom, Theme.Spacing.s)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.Colors.background)
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(Theme.Colors.separator)
                .frame(height: Theme.Stroke.hairline)
        }
        .textCase(nil)
        .listRowInsets(EdgeInsets())
        .listRowBackground(Color.clear)
        .listRowSeparator(.hidden)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
        .accessibilityLabel([SaliDay.title(for: date), detail].compactMap { $0 }.joined(separator: ", "))
    }
}

/// How a day is named, in one place, so no two lists in the app can call the same Tuesday two things.
enum SaliDay {
    static func title(for date: Date?) -> String {
        // The decoder's unreadable-timestamp sentinel is as undated as nil: without this it would open a
        // phantom "1 Jan" group at the bottom of the list, stated as confidently as a real day.
        guard let date, !date.saliIsUnknownTime else { return "Undated" }
        let calendar = Calendar.current
        if calendar.isDateInToday(date) { return "Today" }
        if calendar.isDateInYesterday(date) { return "Yesterday" }
        if let days = calendar.dateComponents([.day], from: date, to: Date()).day, days < 7 {
            return date.formatted(.dateTime.weekday(.wide))
        }
        return date.formatted(.dateTime.weekday(.abbreviated).day().month(.abbreviated))
    }
}

// MARK: - Feed row

/// One event on the day spine.
///
/// Rank is carried by the rail marker and the type ramp, not by colour: a milestone gets a filled node and
/// body-weight ink, a routine step gets a hairline tick and footnote-weight tertiary ink. Every row shares
/// one rail x-position AND one right-aligned time column, so the feed reads as a single timeline with two
/// true vertical edges no matter which ranks are on screen.
private struct FeedEventRow: View {
    let event: SaliEvent
    let rank: FeedRank
    let showsTechnical: Bool
    let onTap: () -> Void

    /// The rail gutter. Fixed, so every marker in the feed sits on exactly one vertical line.
    private static let railWidth: CGFloat = 22

    /// The time gutter. Scaled rather than fixed, so the column still lines up at larger type sizes.
    @ScaledMetric(relativeTo: .caption2) private var timeWidth: CGFloat = 42

    var body: some View {
        Button(action: onTap) {
            HStack(alignment: .top, spacing: Theme.Spacing.m) {
                rail
                VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                    Text(event.humanSummary)
                        .font(titleFont)
                        .foregroundStyle(titleInk)
                        .fixedSize(horizontal: false, vertical: true)
                        .multilineTextAlignment(.leading)
                    if showsTechnical {
                        Text(technicalLine)
                            .font(Theme.Typography.monoSmall)
                            .foregroundStyle(Theme.Colors.tertiaryText)
                            .lineLimit(2)
                    }
                }
                Spacer(minLength: Theme.Spacing.s)
                Text(event.timestamp.map { $0.formatted(.dateTime.hour().minute()) } ?? "")
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .monospacedDigit()
                    .lineLimit(1)
                    .frame(minWidth: timeWidth, alignment: .trailing)
            }
            .padding(.vertical, rank == .routine ? Theme.Spacing.xs : Theme.Spacing.s)
            .frame(minHeight: 44, alignment: .top)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLabel)
        .accessibilityHint("Opens where this happened")
    }

    /// The marker column: a continuous hairline with one node per event. Milestones interrupt the line with
    /// a filled node; routine rows barely touch it.
    private var rail: some View {
        ZStack(alignment: .top) {
            Rectangle()
                .fill(Theme.Colors.separator)
                .frame(width: Theme.Stroke.hairline)
                .frame(maxHeight: .infinity)
            marker
                .padding(.top, 5)
        }
        .frame(width: Self.railWidth)
        .accessibilityHidden(true)
    }

    @ViewBuilder
    private var marker: some View {
        switch rank {
        case .milestone:
            Circle()
                .fill(Theme.Colors.primaryText)
                .frame(width: 7, height: 7)
                .background(
                    Circle().fill(Theme.Colors.background).frame(width: 13, height: 13)
                )
        case .notable:
            Circle()
                .strokeBorder(Theme.Colors.secondaryText, lineWidth: Theme.Stroke.hairline)
                .frame(width: 7, height: 7)
                .background(
                    Circle().fill(Theme.Colors.background).frame(width: 13, height: 13)
                )
        case .routine:
            Rectangle()
                .fill(Theme.Colors.borderStrong)
                .frame(width: 5, height: Theme.Stroke.hairline)
        }
    }

    private var titleFont: Font {
        switch rank {
        case .milestone: Theme.Typography.body.weight(.medium)
        case .notable:   Theme.Typography.callout
        case .routine:   Theme.Typography.footnote
        }
    }

    private var titleInk: Color {
        switch rank {
        case .milestone: Theme.Colors.primaryText
        case .notable:   Theme.Colors.secondaryText
        case .routine:   Theme.Colors.tertiaryText
        }
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
        var label = rank == .milestone ? "Milestone. \(event.humanSummary)" : event.humanSummary
        if let timestamp = event.timestamp { label += ", \(timestamp.saliRelative)" }
        if showsTechnical { label += ". \(technicalLine)" }
        return label
    }
}

#Preview {
    NowView().environmentObject(AppState())
}
