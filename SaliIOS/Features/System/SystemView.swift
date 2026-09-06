import SwiftUI
import Charts

// System — one honest picture of the machine Sali runs on.
//
// Everything here is a real reading from `GET /api/v1/system` and `GET /api/v1/resource-incidents`; nothing
// is inferred and nothing is invented. Three rules hold it together:
//
//  1. **A metric that can't be read is an em dash and an empty track**, never a zero. The backend already
//     degrades an unreadable probe to `null` (`_resource_view`), and the screen has to preserve that
//     honesty all the way to the pixel — a 0% bar and an unread bar must never look alike.
//  2. **Colour is signal.** Six healthy gauges are six INK bars, not six green ones; a subsystem that is up
//     is a plain pill. `warn`/`danger` are spent only on the readings that actually want a human, so on the
//     day something is wrong there is exactly one coloured thing on screen and it is the right one.
//  3. **A transient failure never blanks the dashboard.** The last known-good reading stays up, labelled as
//     stale, until a fetch succeeds. Only a first load with nothing behind it can show an error.

// MARK: - Forgiving decode helpers

/// Reads that CANNOT throw. Named with a `sys` prefix so they stay local to this screen's wire types.
///
/// The shared `APIClient.decoder` is strict on purpose, but strictness at the container level is what turns
/// one unexpected field into a blank screen. Every field below is read independently: a bad field degrades
/// itself and nothing else.
private extension KeyedDecodingContainer {
    func sysString(_ key: Key) -> String? {
        guard let value = try? decodeIfPresent(String.self, forKey: key),
              !value.isEmpty else { return nil }
        return value
    }
    func sysBool(_ key: Key) -> Bool? {
        guard let value = try? decodeIfPresent(Bool.self, forKey: key) else { return nil }
        return value
    }
    func sysInt(_ key: Key) -> Int? {
        guard let value = try? decodeIfPresent(Int.self, forKey: key) else { return nil }
        return value
    }
    /// A timestamp that degrades to `nil` rather than throwing — see `SystemWireDate`.
    func sysDate(_ key: Key) -> Date? {
        if let text = sysString(key) { return SystemWireDate.parse(text) }
        if let seconds = try? decodeIfPresent(Double.self, forKey: key) {
            return Date(timeIntervalSince1970: seconds)
        }
        return nil
    }
}

/// Date parsing that DEGRADES A FIELD instead of failing a screen.
///
/// The shared `APIClient.decoder` throws on any stamp it can't read, which means a single timezone-naive or
/// space-separated timestamp takes down every row that contains it — the exact bug that has blanked two
/// screens in this app already. Nothing here throws: the stamp is read as a string, tried against every
/// shape the host has ever emitted, and left as `nil` if none of them fit. A missing "when" costs the user
/// one line of metadata; a thrown error costs them the screen.
enum SystemWireDate {
    static func parse(_ text: String) -> Date? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }

        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = withFraction.date(from: trimmed) { return date }

        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        if let date = plain.date(from: trimmed) { return date }

        // Everything the strict parsers reject: a space instead of the "T", and a timezone-naive stamp
        // (a Postgres `timestamp` rather than `timestamptz`), which the host means as UTC.
        for format in ["yyyy-MM-dd HH:mm:ssXXXXX", "yyyy-MM-dd HH:mm:ss.SSSSSSXXXXX",
                       "yyyy-MM-dd'T'HH:mm:ss.SSSSSS", "yyyy-MM-dd'T'HH:mm:ss",
                       "yyyy-MM-dd HH:mm:ss.SSSSSS", "yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd"] {
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "en_US_POSIX")
            formatter.timeZone = TimeZone(secondsFromGMT: 0)
            formatter.dateFormat = format
            if let date = formatter.date(from: trimmed) { return date }
        }
        return nil
    }
}

// MARK: - Wire shape

/// `GET /api/v1/system`, decoded so that NOTHING can throw.
///
/// The route composes three independent best-effort blocks (resources / subsystem health / model), and each
/// of them can come back as its own error object — `{"available": false, "error": "…"}` for a failed
/// hardware probe, `{"all_ok": false, "error": "…"}` for a failed health check. The old screen decoded the
/// strict `SystemHealth` model, so a single unexpected field anywhere took the ENTIRE screen down to an
/// error state, and the `error` strings the backend went to the trouble of sending were dropped on the
/// floor. Here every field is read independently, and the reasons are kept so the screen can say *why* a
/// block is missing instead of showing a silent row of dashes.
struct SystemReport {
    let model: String?
    let resources: ResourceSnapshot?
    let subsystems: SubsystemHealth?
    let connections: Int?
    let resourceError: String?
    let subsystemError: String?
}

extension SystemReport: Decodable {
    private enum Keys: String, CodingKey {
        case model, resources, health
        case connections = "websocket_connections"
    }
    private enum ErrorKeys: String, CodingKey { case error }

    init(from decoder: Decoder) throws {
        let root = try decoder.container(keyedBy: Keys.self)
        model = root.sysString(.model)
        resources = try? root.decodeIfPresent(ResourceSnapshot.self, forKey: .resources)
        subsystems = try? root.decodeIfPresent(SubsystemHealth.self, forKey: .health)
        connections = root.sysInt(.connections)
        resourceError = SystemReport.errorString(in: root, at: .resources)
        subsystemError = SystemReport.errorString(in: root, at: .health)
    }

    /// The optional `error` string a best-effort block carries when it failed.
    private static func errorString(in container: KeyedDecodingContainer<Keys>, at key: Keys) -> String? {
        guard let nested = try? container.nestedContainer(keyedBy: ErrorKeys.self, forKey: key) else {
            return nil
        }
        return nested.sysString(.error)
    }
}

/// One row of `GET /api/v1/resource-incidents` → `resource_incident` (migration 0043).
///
/// Decoded here rather than through the shared `ResourceIncident` because three things about that model
/// were wrong for this screen, all of them real defects: the row carries a REAL `id` (uuid) which the
/// shared model threw away in favour of a computed `"kind-timestamp"` that collides between two incidents
/// of the same kind; `created_at` went through the strict shared date strategy, so ONE unparseable stamp
/// discarded the entire incidents list; and `observed` — the measured values at the moment of the incident,
/// i.e. the whole evidential point of the record (§21, "evidence-based, never hallucinated fear") — was not
/// decoded at all.
struct SystemIncident: Identifiable {
    let id: String
    let kind: String
    let severity: String
    let workload: String?
    let mitigation: String?
    let resolved: Bool
    let createdAt: Date?
    /// The measured resource values at the time. A `jsonb` column, so it arrives as an object — or, if the
    /// pool has no json codec registered, as a STRING containing that object. Both are read.
    let observed: [String: JSONValue]
}

extension SystemIncident: Decodable {
    private enum Keys: String, CodingKey {
        case id, kind, severity, workload, mitigation, resolved, observed
        case createdAt = "created_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        kind = c.sysString(.kind) ?? "incident"
        severity = c.sysString(.severity) ?? "high"
        workload = c.sysString(.workload)
        mitigation = c.sysString(.mitigation)
        resolved = c.sysBool(.resolved) ?? false
        createdAt = c.sysDate(.createdAt)
        observed = SystemIncident.readObserved(c)

        // A real uuid when the row has one. The fallback is derived from the row's own content rather than
        // a fresh UUID, so an id stays stable across the five-second poll and SwiftUI doesn't tear down and
        // rebuild every card on every refresh.
        id = c.sysString(.id)
            ?? "\(kind)|\(severity)|\(createdAt?.timeIntervalSince1970 ?? 0)|\(workload ?? "")"
    }

    private static func readObserved(_ c: KeyedDecodingContainer<Keys>) -> [String: JSONValue] {
        if let object = try? c.decodeIfPresent([String: JSONValue].self, forKey: .observed) {
            return object
        }
        // jsonb delivered as a string — parse it rather than losing the evidence.
        if let raw = c.sysString(.observed), let data = raw.data(using: .utf8),
           let object = try? JSONDecoder().decode([String: JSONValue].self, from: data) {
            return object
        }
        return [:]
    }

    /// The plain-language reasons the runtime recorded alongside the numbers ("background/curiosity shed
    /// under critical pressure"). It arrives as a JSON array, which the old renderer had no case for, so
    /// the single most readable thing in the record was the one part never shown.
    var reasons: [String] {
        guard case .array(let items)? = observed["reasons"] else { return [] }
        return items.compactMap {
            guard case .string(let text) = $0, !text.isEmpty else { return nil }
            return text
        }
    }

    /// The measured values as short, readable lines ("vram used 97%", "gpu temp 88°C"). Keys are sorted so
    /// the order can't shuffle between polls.
    var evidence: [String] {
        observed.keys.sorted().compactMap { SystemIncident.describe(key: $0, value: observed[$0]) }
    }

    private static func describe(key: String, value: JSONValue?) -> String? {
        guard let value else { return nil }
        // `reasons` is rendered as prose above; `ok` is the probe's own health flag, not a reading.
        guard key != "reasons", key != "ok" else { return nil }
        let label = key
            .replacingOccurrences(of: "_frac", with: "")
            .replacingOccurrences(of: "_pct", with: "")
            .replacingOccurrences(of: "_c", with: "")
            .replacingOccurrences(of: "_", with: " ")
        switch value {
        case .number(let n):
            if key.hasSuffix("_frac") { return "\(label) \(Int((n * 100).rounded()))%" }
            if key.hasSuffix("_pct")  { return "\(label) \(Int(n.rounded()))%" }
            if key.hasSuffix("_c")    { return "\(label) \(Int(n.rounded()))°C" }
            return "\(label) \(SystemIncident.trim(n))"
        case .string(let s):         return s.isEmpty ? nil : "\(label) \(s)"
        case .bool(let b):           return "\(label) \(b ? "yes" : "no")"
        case .null, .array, .object: return nil
        }
    }

    private static func trim(_ value: Double) -> String {
        value == value.rounded() ? String(Int(value)) : String(format: "%.2f", value)
    }
}

/// `{"incidents": [...], "counts": {"total": n, "open": n, "by_kind": {…}}}`.
///
/// Decoded ELEMENT BY ELEMENT: one malformed incident is dropped, never the whole section — the failure
/// mode that has blanked this screen before. `counts` was previously ignored entirely, so the header could
/// only ever describe the 25 rows it happened to be holding, not the ledger behind them.
struct IncidentsReport: Decodable {
    let incidents: [SystemIncident]
    let total: Int?
    let open: Int?

    private enum Keys: String, CodingKey { case incidents, counts }
    private enum CountKeys: String, CodingKey { case total, open }

    init(from decoder: Decoder) throws {
        if let root = try? decoder.container(keyedBy: Keys.self), root.contains(.incidents) {
            incidents = IncidentsReport.lenientRows(in: root)
            if let counts = try? root.nestedContainer(keyedBy: CountKeys.self, forKey: .counts) {
                total = counts.sysInt(.total)
                open = counts.sysInt(.open)
            } else {
                total = nil
                open = nil
            }
        } else {
            // A bare array, in case the shape ever flattens.
            var unkeyed = try decoder.unkeyedContainer()
            incidents = IncidentsReport.drain(&unkeyed)
            total = nil
            open = nil
        }
    }

    private static func lenientRows(in container: KeyedDecodingContainer<Keys>) -> [SystemIncident] {
        guard var unkeyed = try? container.nestedUnkeyedContainer(forKey: .incidents) else { return [] }
        return drain(&unkeyed)
    }

    private static func drain(_ unkeyed: inout UnkeyedDecodingContainer) -> [SystemIncident] {
        var rows: [SystemIncident] = []
        while !unkeyed.isAtEnd {
            if let row = try? unkeyed.decode(SystemIncident.self) {
                rows.append(row)
            } else if (try? unkeyed.decode(JSONValue.self)) == nil {
                break   // can't even skip it — stop rather than spin
            }
        }
        return rows
    }
}

// MARK: - View model

/// Drives `SystemView`: loads `/system` + `/resource-incidents`, keeps a bounded rolling buffer of VRAM/GPU
/// samples for the trend, and reacts to live `resource.*` events. A transient refresh failure never blanks
/// the screen — the last known-good reading stays up and is labelled stale (§16 "keep last-known, retry").
@MainActor
final class SystemViewModel: ObservableObject {
    struct ResourcePoint: Identifiable {
        let id = UUID()
        let at: Date
        let vramPct: Double?
        let gpuPct: Double?
    }

    /// `Void` because the payload lives in `report` — the loadable only tracks whether there has EVER been
    /// a reading, which is the one thing that decides skeleton vs. content vs. error.
    @Published private(set) var state: Loadable<Void> = .idle
    @Published private(set) var report: SystemReport?
    @Published private(set) var incidents: [SystemIncident] = []
    @Published private(set) var incidentTotal: Int?
    @Published private(set) var incidentOpen: Int?
    @Published private(set) var incidentsErrorMessage: String?
    @Published private(set) var history: [ResourcePoint] = []

    /// When the reading on screen was actually taken — the difference between "the machine is idle" and
    /// "we haven't heard from the machine in four minutes", which the old screen could not tell apart.
    @Published private(set) var readingTakenAt: Date?
    /// Set when a refresh fails while a good reading is still on screen. The content stays; only the label
    /// changes (item 2 — a transient error keeps last-known content, and says so).
    @Published private(set) var staleReason: String?

    /// Only the samples with a real VRAM reading — never plot a fabricated zero for an unread metric.
    var vramSeries: [(at: Date, pct: Double)] {
        history.compactMap { point in point.vramPct.map { (at: point.at, pct: $0) } }
    }

    private var api: APIClient?
    private let maxHistory = 30
    private let pollInterval: UInt64 = 5_000_000_000   // 5s — a modest cadence (§40), not every second

    func configure(api: APIClient) {
        if self.api == nil { self.api = api }
    }

    /// Loads once, then refreshes on a modest interval for as long as the caller's `.task` stays alive —
    /// SwiftUI cancels the enclosing task automatically when the view disappears, which stops this loop.
    func run() async {
        await loadInitial()
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: pollInterval)
            if Task.isCancelled { break }
            await refresh()
        }
    }

    func loadInitial() async {
        if report == nil { state = .loading }
        await refresh()
        await refreshIncidents()
    }

    func refresh() async {
        guard let api else { return }
        do {
            let latest: SystemReport = try await api.get("system")
            report = latest
            readingTakenAt = Date()
            staleReason = nil
            state = .loaded(())
            appendSample(latest.resources)
        } catch {
            // Keep the last known-good reading; a blip must not blank the dashboard. Only a first load with
            // nothing behind it is allowed to become an error state.
            if report == nil {
                state = .failed((error as? APIError)?.errorDescription ?? "Couldn't read the host.")
            } else {
                staleReason = (error as? APIError)?.errorDescription ?? "Couldn't reach the host."
            }
        }
    }

    func refreshIncidents() async {
        guard let api else { return }
        do {
            let response: IncidentsReport = try await api.get("resource-incidents")
            incidents = response.incidents
            incidentTotal = response.total
            incidentOpen = response.open
            incidentsErrorMessage = nil
        } catch {
            // Same posture as the main reading: a failed refresh keeps whatever incidents are already up.
            if incidents.isEmpty {
                incidentsErrorMessage = (error as? APIError)?.errorDescription ?? "Couldn't load incidents."
            }
        }
    }

    func handleLiveEvent(_ event: SaliEvent) {
        // `resource.incident_recorded` is the one the runtime actually emits today; matching the family by
        // prefix as well means a future `resource.state_changed` lands here without another release.
        guard event.type == .resourceIncident || event.type == .resourceState
                || event.rawType.hasPrefix("resource.") else { return }
        Task { await refresh(); await refreshIncidents() }
    }

    private func appendSample(_ resources: ResourceSnapshot?) {
        // A sample is worth keeping whenever the host answered with a reading — a classified state, OR at
        // least one real metric. Requiring `state` alone silently dropped every sample on a host whose
        // classifier hadn't produced one yet, and the trend card then sat on "gathering samples" forever.
        guard let resources,
              resources.state != nil || resources.vramUsedPct != nil || resources.gpuUtilPct != nil
        else { return }
        history.append(ResourcePoint(at: Date(), vramPct: resources.vramUsedPct, gpuPct: resources.gpuUtilPct))
        if history.count > maxHistory { history.removeFirst(history.count - maxHistory) }
    }
}

// MARK: - Screen

/// System Health (§18/§19) — one consolidated, real-time view of the host Sali runs on, tied directly to
/// Prompt 12's resource-stewardship ladder: the state banner, gauges, and incidents here are the same
/// deterministic signals that drive `ResourceAuthority` server-side (never a second, invented model of
/// "how Sali is doing"). Pushed as a `NavigationLink` destination from `MoreView` (see `RootView.swift`),
/// so — like `NotificationsView` — this does not open its own `NavigationStack`.
struct SystemView: View {
    @EnvironmentObject private var appState: AppState
    @StateObject private var viewModel = SystemViewModel()

    var body: some View {
        Group {
            switch viewModel.state {
            case .idle, .loading:
                systemSkeleton
            case .failed(let message):
                // `refresh()` only ever sets `.failed` when there is no reading to fall back on — once one
                // succeeds, a later transient error keeps `content` on screen and marks it stale instead.
                ErrorStateView(message) { Task { await viewModel.refresh() } }
            case .loaded:
                content
            }
        }
        .navigationTitle("System")
        .toolbar {
            // Presence, not the socket. This screen is about the MACHINE Sali runs on — its readings come
            // over REST, so a socket badge here answered a question the screen never asks, in a second
            // vocabulary ("Live") for the same corner where every other screen says "IDLE".
            PresenceToolbarItem()
        }
        .task {
            viewModel.configure(api: appState.api)
            await viewModel.run()
        }
        .refreshable {
            await viewModel.refresh()
            await viewModel.refreshIncidents()
        }
        .onChange(of: appState.latestEvent?.id) { _, _ in
            if let event = appState.latestEvent { viewModel.handleLiveEvent(event) }
        }
    }

    private static let metricColumns = [GridItem(.flexible(), spacing: Theme.Spacing.m),
                                        GridItem(.flexible(), spacing: Theme.Spacing.m)]

    /// The first-load placeholder, laid out exactly like `content`: state banner, the two-up metric grid,
    /// the runtime card, then the trend card (item 1). Nothing here moves under
    /// `accessibilityReduceMotion` — `SaliSkeleton` gates its own sweep and never sets its driving state.
    private var systemSkeleton: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Spacing.blockGap) {
                SaliSkeletonCard(lines: 2, showsPill: false)
                LazyVGrid(columns: Self.metricColumns, spacing: Theme.Spacing.m) {
                    ForEach(0..<6, id: \.self) { _ in
                        SaliSkeletonCard(lines: 1, showsPill: false, showsBar: true)
                    }
                }
                SaliSkeletonCard(lines: 2, showsPill: false)
                SaliSkeletonCard(lines: 3, showsPill: false)
            }
            .padding(Theme.Spacing.screenMargin)
        }
        .scrollDisabled(true)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.Colors.background)
        .allowsHitTesting(false)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Reading the machine")
    }

    // MARK: Loaded content

    /// `spacing: 0` on purpose — `SectionHeader` owns its own vertical rhythm, so a stack spacing here would
    /// compound with it and every block would drift further apart than the system intends.
    private var content: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                if let report = viewModel.report {
                    StateBanner(resources: report.resources,
                                probeError: report.resourceError,
                                takenAt: viewModel.readingTakenAt,
                                staleReason: viewModel.staleReason)

                    SectionHeader("Host", subtitle: "Live readings from the machine Sali runs on")
                    metricsGrid(report.resources)

                    SectionHeader("Runtime", subtitle: "What is loaded, and what is still answering")
                    runtimeCard(report)
                }

                SectionHeader("VRAM trend", subtitle: trendSubtitle)
                trendCard

                SectionHeader("Incidents", subtitle: incidentsSubtitle)
                incidentsBlock
                SectionFooter(
                    "Recorded when the host is genuinely endangered, so Sali can adapt a plan instead of "
                    + "rediscovering the same mistake.",
                    detail: viewModel.incidents.isEmpty ? nil : "\(viewModel.incidents.count) shown")
            }
            .padding(.horizontal, Theme.Spacing.screenMargin)
            .padding(.bottom, Theme.Spacing.xl)
        }
        .background(Theme.Colors.background)
    }

    // MARK: Host

    private func metricsGrid(_ resources: ResourceSnapshot?) -> some View {
        LazyVGrid(columns: Self.metricColumns, spacing: Theme.Spacing.m) {
            MetricCard(title: "GPU", systemImage: "cpu",
                       value: resources?.gpuUtilPct, unit: .percent, detail: nil,
                       warn: nil, danger: nil, cap: 100)
            MetricCard(title: "VRAM", systemImage: "memorychip",
                       value: resources?.vramUsedPct, unit: .percent,
                       detail: Self.vramDetail(resources),
                       warn: GaugeThresholds.vramWarn, danger: GaugeThresholds.vramDanger, cap: 100)
            MetricCard(title: "RAM", systemImage: "memorychip.fill",
                       value: resources?.ramUsedPct, unit: .percent, detail: nil,
                       warn: GaugeThresholds.ramWarn, danger: GaugeThresholds.ramDanger, cap: 100)
            // MISWIRED until now. Every other metric is converted to a percentage by the API's
            // `_resource_view`, but `cpu_load` is passed through as the RAW `cpu_load_per_core` ratio
            // (1.0 = one core fully busy). The screen formatted it with the percent formatter, so a real
            // reading of 0.003 rendered as "0%" and the gauge — scaled against a 400 "percent" ceiling —
            // never moved off the stop, whatever the machine was doing.
            MetricCard(title: "CPU load", systemImage: "gauge.with.needle",
                       value: resources?.cpuLoad, unit: .load, detail: "per core · 1.00 is one core busy",
                       warn: GaugeThresholds.loadWarn, danger: GaugeThresholds.loadDanger,
                       cap: GaugeThresholds.loadDanger)
            MetricCard(title: "Disk", systemImage: "internaldrive",
                       value: resources?.diskUsedPct, unit: .percent, detail: nil,
                       warn: GaugeThresholds.diskWarn, danger: GaugeThresholds.diskDanger, cap: 100)
            // The field behind this is `gpu_temp_c`, so the card says so rather than implying a board-wide
            // reading it never had.
            MetricCard(title: "Temperature", systemImage: "thermometer.medium",
                       value: resources?.temperatureC, unit: .celsius, detail: "GPU core",
                       warn: GaugeThresholds.tempWarnC, danger: GaugeThresholds.tempDangerC, cap: 100)
        }
    }

    // MARK: Runtime

    private func runtimeCard(_ report: SystemReport) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            // A model id is an identifier, not prose — mono, so "qwen3:32b-q4_K_M" stays readable.
            DetailRow(label: "Model", value: report.model ?? "—",
                      font: Theme.Typography.monoSmall,
                      ink: report.model == nil ? Theme.Colors.tertiaryText : Theme.Colors.primaryText)
            DetailRow(label: "Live connections", value: report.connections.map(String.init) ?? "—",
                      font: Theme.Typography.numeralSmall,
                      ink: Theme.Colors.tertiaryText)

            Rectangle()
                .fill(Theme.Colors.separator)
                .frame(height: Theme.Stroke.hairline)
                .padding(.vertical, Theme.Spacing.xs)

            // Four booleans read as four aligned rows rather than a wrapping raft of pills: at large
            // Dynamic Type "Embedder unknown" is wider than half the screen, and a pill raft either
            // overflows its column or breaks a capsule across two lines. Rows always fit, and they line up
            // with the two above them so the whole card is one column of facts.
            subsystemRow("Datastore", report.subsystems?.datastore)
            subsystemRow("Model service", report.subsystems?.model)
            subsystemRow("Embedder", report.subsystems?.embedder)
            subsystemRow("Internet", report.subsystems?.internet)

            if hasUnnamedFailure(report.subsystems) {
                DetailRow(label: "Another subsystem", value: "Degraded",
                          font: Theme.Typography.footnote.weight(.medium), ink: Theme.Colors.warn)
                Text("The host reports a degraded subsystem that this view isn't given the name of.")
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let reason = report.subsystemError {
                Text(reason)
                    .font(Theme.Typography.monoSmall)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .saliCard()
    }

    /// A subsystem that is UP is the expected case, so it reads in plain ink; only one that is DOWN is
    /// telling the user something (item 12). The meaning is in the WORD, not only the colour, so the row
    /// reads correctly with no colour perception at all.
    private func subsystemRow(_ name: String, _ isUp: Bool?) -> DetailRow {
        switch isUp {
        case .some(true):  DetailRow(label: name, value: "Up",
                                     font: Theme.Typography.footnote, ink: Theme.Colors.primaryText)
        case .some(false): DetailRow(label: name, value: "Down",
                                     font: Theme.Typography.footnote.weight(.semibold),
                                     ink: Theme.Colors.danger)
        case .none:        DetailRow(label: name, value: "Unknown",
                                     font: Theme.Typography.footnote, ink: Theme.Colors.tertiaryText)
        }
    }

    /// `Health.all_ok` is computed over FIVE subsystems (datastore, model, embedder, **perception**,
    /// internet), but `/system` forwards only four of them — so a host with perception down answers
    /// `all_ok: false` alongside four perfectly healthy flags, and the screen used to show four calm pills
    /// and imply everything was fine. The disagreement is stated rather than swallowed. Nothing is guessed:
    /// the payload does not name the failing subsystem, so neither does this.
    private func hasUnnamedFailure(_ health: SubsystemHealth?) -> Bool {
        guard health?.allOk == false else { return false }
        return [health?.datastore, health?.model, health?.embedder, health?.internet]
            .allSatisfy { $0 != false }
    }

    // MARK: Trend

    private var trendSubtitle: String {
        let count = viewModel.vramSeries.count
        guard count >= 2 else { return "One sample every five seconds" }
        return "\(count) samples, one every five seconds"
    }

    private var trendCard: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.m) {
            if viewModel.vramSeries.count >= 2 {
                VRAMChart(points: viewModel.vramSeries)
                    .frame(height: 140)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(chartAccessibilityLabel)
                if let latest = viewModel.history.last {
                    HStack(spacing: Theme.Spacing.l) {
                        readout("VRAM", latest.vramPct)
                        readout("GPU", latest.gpuPct)
                        Spacer(minLength: 0)
                    }
                }
            } else {
                // The same height as the chart, so the card doesn't jump the moment a second sample lands.
                Text("Gathering samples — the trend appears once there are two readings.")
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, minHeight: 140)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .saliCard()
    }

    private func readout(_ label: String, _ value: Double?) -> some View {
        HStack(spacing: Theme.Spacing.xs) {
            Text(label)
                .font(Theme.Typography.metadata)
                .foregroundStyle(Theme.Colors.tertiaryText)
            Text(MetricUnit.percent.format(value))
                .font(Theme.Typography.numeralSmall)
                .foregroundStyle(value == nil ? Theme.Colors.tertiaryText : Theme.Colors.primaryText)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(label) \(MetricUnit.percent.spoken(value))")
    }

    private var chartAccessibilityLabel: String {
        let values = viewModel.vramSeries.map(\.pct)
        guard let last = values.last else { return "VRAM usage trend, no data yet." }
        let first = values.first ?? last
        let direction = last > first ? "rising" : (last < first ? "falling" : "steady")
        return "VRAM usage trend over \(values.count) samples, \(direction), "
            + "most recently \(Int(last.rounded())) percent."
    }

    // MARK: Incidents

    private var incidentsSubtitle: String {
        if let total = viewModel.incidentTotal {
            let recorded = total == 1 ? "1 recorded" : "\(total) recorded"
            let open = viewModel.incidentOpen ?? 0
            return open > 0 ? "\(recorded) · \(open) unresolved" : "\(recorded) · none unresolved"
        }
        return viewModel.incidents.isEmpty ? "Nothing recorded yet" : "\(viewModel.incidents.count) recorded"
    }

    @ViewBuilder private var incidentsBlock: some View {
        if let message = viewModel.incidentsErrorMessage, viewModel.incidents.isEmpty {
            VStack(alignment: .leading, spacing: Theme.Spacing.m) {
                Text(message)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
                Button("Try again") { Task { await viewModel.refreshIncidents() } }
                    .buttonStyle(SaliPrimaryButtonStyle())
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .saliCard()
        } else if viewModel.incidents.isEmpty {
            // Not a green shield. Nothing is being asked of the user, so nothing here raises its voice.
            Text("No incidents recorded. Sali has not had to protect the host from anything yet.")
                .font(Theme.Typography.callout)
                .foregroundStyle(Theme.Colors.secondaryText)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .saliCard()
        } else {
            LazyVStack(spacing: Theme.Spacing.m) {
                ForEach(viewModel.incidents) { incident in
                    IncidentRow(incident: incident)
                }
            }
        }
    }

    // MARK: Formatting

    private static func vramDetail(_ resources: ResourceSnapshot?) -> String? {
        guard let used = resources?.vramUsedMB, let total = resources?.vramTotalMB, total > 0 else {
            return nil
        }
        return "\(Int(used)) / \(Int(total)) MB"
    }
}

/// Mirrors `ResourceBudget` defaults in `sali/runtime/resources.py` — used only to tint the gauges
/// consistently with the same thresholds the runtime itself classifies against. The state banner's level
/// still comes authoritatively from `resources.state`, never from these local numbers.
private enum GaugeThresholds {
    static let vramWarn = 85.0, vramDanger = 92.0
    static let ramWarn = 85.0, ramDanger = 93.0
    static let diskWarn = 90.0, diskDanger = 96.0
    static let tempWarnC = 82.0, tempDangerC = 87.0
    // Per-core load ratios, matching the units the API actually sends (NOT percentages).
    static let loadWarn = 2.0, loadDanger = 4.0
}

/// How a reading is written out. The nil case is an EM DASH in every unit — a metric the host could not
/// read must never be able to look like a zero.
private enum MetricUnit {
    case percent, celsius
    /// A per-core load RATIO, where 1.0 is one core fully busy. Not a percentage, and this screen used to
    /// print it as one — see the CPU card in `metricsGrid`.
    case load

    func format(_ value: Double?) -> String {
        guard let value else { return "—" }
        switch self {
        case .percent: return "\(Int(value.rounded()))%"
        case .celsius: return "\(Int(value.rounded()))°C"
        case .load:    return String(format: "%.2f", value)
        }
    }

    func spoken(_ value: Double?) -> String {
        guard let value else { return "no reading" }
        switch self {
        case .percent: return "\(Int(value.rounded())) percent"
        case .celsius: return "\(Int(value.rounded())) degrees Celsius"
        case .load:    return String(format: "%.2f per core", value)
        }
    }
}

// MARK: - State banner

/// The screen's verdict, and the only element allowed to be loud.
///
/// At rest it is an ordinary card — ink on `surfaceRaised`, no tint, no motion. It escalates only for the
/// levels that actually want a human (`high` and above): a muted wash, an emphasised edge, and one slow
/// breath on that edge at the app's single tempo. `safe` and `elevated` are both "nothing is being asked of
/// you", so the icon and the wording carry the difference and no colour is spent (item 12).
private struct StateBanner: View {
    let resources: ResourceSnapshot?
    let probeError: String?
    let takenAt: Date?
    let staleReason: String?

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .title2) private var glyphSize: CGFloat = 22
    @State private var breathing = false

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                Image(systemName: icon)
                    .font(.system(size: glyphSize, weight: .regular))
                    .foregroundStyle(accent)
                    .accessibilityHidden(true)
                Text(title)
                    .font(Theme.Typography.title)
                    .foregroundStyle(escalated ? accent : Theme.Colors.primaryText)
                Spacer(minLength: Theme.Spacing.s)
                if resources?.preservation == true {
                    StatusPill("Preserving", color: Theme.Colors.danger)
                }
            }

            Text(meaning)
                .font(Theme.Typography.body)
                .foregroundStyle(Theme.Colors.secondaryText)
                .fixedSize(horizontal: false, vertical: true)

            // Why the probe failed, when it did — the backend sends a reason and the screen used to bin it.
            if let probeError, !known {
                Text(probeError)
                    .font(Theme.Typography.monoSmall)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
            }

            provenance
        }
        .padding(Theme.Spacing.l)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(escalated ? accent.opacity(0.10) : Theme.Colors.surfaceRaised)
        .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.m, style: .continuous))
        .saliHairline(radius: Theme.Radius.m,
                      color: escalated ? accent.opacity(breathing ? 0.55 : 0.28) : Theme.Colors.border,
                      width: escalated ? Theme.Stroke.emphasis : Theme.Stroke.hairline)
        .padding(.top, Theme.Spacing.l)
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle), value: resources?.state)
        .onAppear(perform: syncBreathing)
        .onChange(of: escalated) { _, _ in syncBreathing() }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityText)
    }

    /// One quiet line of provenance: when this reading was taken, and whether it is still current. The
    /// relative stamp is a live `Text` style, so it keeps counting on its own between polls.
    @ViewBuilder private var provenance: some View {
        if let staleReason {
            Group {
                if let takenAt {
                    Text("\(staleReason) Showing the reading from ") + Text(takenAt, style: .relative)
                        + Text(" ago.")
                } else {
                    Text(staleReason)
                }
            }
            .font(Theme.Typography.metadata)
            .foregroundStyle(Theme.Colors.warn)
            .fixedSize(horizontal: false, vertical: true)
        } else if let takenAt {
            (Text("Read ") + Text(takenAt, style: .relative) + Text(" ago"))
                .font(Theme.Typography.metadata)
                .foregroundStyle(Theme.Colors.tertiaryText)
        }
    }

    /// The ambient loop is never started under Reduce Motion — the driving state is not set at all, so
    /// there is no repeating animation in the tree to honour or cancel.
    private func syncBreathing() {
        guard !reduceMotion, escalated else { breathing = false; return }
        withAnimation(Theme.Motion.breathing) { breathing = true }
    }

    private var known: Bool { resources?.state != nil }
    private var level: ResourceLevel { resources?.resourceState ?? .safe }
    private var escalated: Bool { known && (level == .high || level == .critical || level == .emergency) }

    private var title: String { known ? level.rawValue.capitalized : "State unknown" }
    private var meaning: String {
        known ? level.meaning
              : "Sali couldn't read host resources just now — this refreshes on its own every few seconds."
    }
    private var accent: Color {
        guard known else { return Theme.Colors.tertiaryText }
        switch level {
        case .safe, .elevated:      return Theme.Colors.secondaryText
        case .high:                 return Theme.Colors.warn
        case .critical, .emergency: return Theme.Colors.danger
        }
    }
    private var icon: String {
        guard known else { return "questionmark.circle" }
        switch level {
        case .safe:      return "checkmark.circle"
        case .elevated:  return "chart.line.uptrend.xyaxis"
        case .high:      return "exclamationmark.triangle"
        case .critical:  return "shield.lefthalf.filled"
        case .emergency: return "exclamationmark.octagon"
        }
    }
    private var accessibilityText: String {
        var parts = ["\(title). \(meaning)."]
        if resources?.preservation == true { parts.append("Preservation mode active.") }
        if let staleReason { parts.append("This reading may be out of date: \(staleReason)") }
        return parts.joined(separator: " ")
    }
}

// MARK: - Metric card

/// One reading: what it is, what it says, and how full it is. The gauge is pinned to the bottom of the card
/// by a `Spacer`, so in a two-up grid every bar sits on the same line however tall its neighbour grew.
private struct MetricCard: View {
    let title: String
    let systemImage: String
    let value: Double?
    let unit: MetricUnit
    let detail: String?
    let warn: Double?
    let danger: Double?
    let cap: Double

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack(spacing: Theme.Spacing.xs) {
                Image(systemName: systemImage)
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .accessibilityHidden(true)
                Text(title)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .lineLimit(1)
            }

            // `numeral` — monospaced digits, so a percentage that changes every five seconds doesn't make
            // the whole card twitch as a 1 becomes a 7.
            Text(unit.format(value))
                .font(Theme.Typography.numeral)
                .foregroundStyle(value == nil ? Theme.Colors.tertiaryText : Theme.Colors.primaryText)
                .lineLimit(1)
                .minimumScaleFactor(0.7)

            if let subtitle {
                Text(subtitle)
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    // Two lines, not one. These captions exist to stop a number being misread — the CPU
                    // card's "per core · 1.00 is one core busy" is the whole reason that tile is legible —
                    // and at one line it truncated to "1.00 is one cor…", cutting the explanation off at
                    // the exact word carrying it. The gauge stays bottom-aligned either way.
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Spacer(minLength: Theme.Spacing.xs)

            GaugeBar(fraction: fraction, tint: tint, reduceMotion: reduceMotion)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .saliCard()
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(title): \(unit.spoken(value))\(subtitle.map { ", \($0)" } ?? "")")
    }

    /// The natural detail if the metric has one; otherwise the threshold — but only once the reading is
    /// close enough to it to matter. Calm at rest, informative the moment it isn't.
    private var subtitle: String? {
        if let detail { return detail }
        // Proportional, so the same rule works for a percentage (85), a temperature (82°C) and a load
        // ratio (2.00) — an absolute "within 10" margin is meaningless for the last of those.
        guard let value, let warn, value >= warn * 0.85 else { return nil }
        return "warn at \(unit.format(warn))"
    }

    /// A 0...1 fraction for the gauge. `cap` lets a metric that can exceed 100% (CPU load) render a sensible
    /// bar instead of clipping silently. `nil` in, `nil` out — never a substituted zero.
    private var fraction: Double? {
        guard let value else { return nil }
        return max(0, min(1, value / max(cap, 1)))
    }

    /// Ink until the runtime's own thresholds say otherwise (item 12). Six healthy gauges used to render as
    /// six coloured bars — a wall that said nothing, and drowned out the one bar that mattered on the day
    /// something was actually wrong.
    private var tint: Color {
        guard let value else { return Theme.Colors.idle }
        if let danger, value >= danger { return Theme.Colors.danger }
        if let warn, value >= warn { return Theme.Colors.warn }
        return Theme.Colors.accent
    }
}

/// The gauge used to fill its track with `surfaceRaised` — the exact colour of the `saliCard` behind it —
/// so the track was invisible and a low reading looked like a broken stub floating in space. The track is
/// now `placeholder`, the one token defined to stay visible on every step of the elevation ramp, and an
/// UNREAD metric renders as a visibly empty track (its value already reads as an em dash), so "no reading"
/// can never masquerade as "zero" (item 13).
private struct GaugeBar: View {
    let fraction: Double?
    let tint: Color
    let reduceMotion: Bool

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Theme.Colors.placeholder)
                if let fraction, fraction > 0 {
                    Capsule()
                        .fill(tint)
                        // A floor of 3pt so a genuine 1% is still a visible mark; exactly zero stays empty,
                        // because an empty track is the truth about a zero reading.
                        .frame(width: max(3, CGFloat(fraction) * geo.size.width))
                }
            }
        }
        .frame(height: 6)
        // The screen's aliveness: bars glide to the new reading each poll instead of snapping to it.
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle), value: fraction)
        .accessibilityHidden(true)
    }
}

// MARK: - Detail row

/// One label-and-value line. Every fact in the Runtime card is one of these, so the values form a single
/// right-aligned column instead of drifting with each row's content.
private struct DetailRow: View {
    let label: String
    let value: String
    let font: Font
    let ink: Color

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
            Text(label)
                .font(Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.secondaryText)
            Spacer(minLength: Theme.Spacing.s)
            Text(value)
                .font(font)
                .foregroundStyle(ink)
                .lineLimit(1)
                .truncationMode(.middle)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(label): \(value)")
    }
}

// MARK: - Chart

private struct VRAMChart: View {
    /// Already non-optional: the series is built by DROPPING unread samples, never by substituting a zero
    /// for one — a fabricated 0% would draw a cliff the machine never had.
    let points: [(at: Date, pct: Double)]

    var body: some View {
        Chart {
            ForEach(Array(points.enumerated()), id: \.offset) { _, point in
                AreaMark(x: .value("Time", point.at), y: .value("VRAM %", point.pct))
                    .foregroundStyle(
                        LinearGradient(colors: [Theme.Colors.accent.opacity(0.18), .clear],
                                       startPoint: .top, endPoint: .bottom))
                    .interpolationMethod(.catmullRom)
                LineMark(x: .value("Time", point.at), y: .value("VRAM %", point.pct))
                    .foregroundStyle(Theme.Colors.accent)
                    .interpolationMethod(.catmullRom)
                    .lineStyle(StrokeStyle(lineWidth: 2, lineCap: .round, lineJoin: .round))
            }
        }
        .chartYScale(domain: 0...100)
        .chartXAxis(.hidden)
        .chartYAxis {
            AxisMarks(position: .leading, values: [0, 50, 100]) { value in
                AxisGridLine().foregroundStyle(Theme.Colors.separator)
                AxisValueLabel {
                    if let intValue = value.as(Int.self) {
                        Text("\(intValue)%")
                            .font(Theme.Typography.metadata)
                            .foregroundStyle(Theme.Colors.tertiaryText)
                    }
                }
            }
        }
    }
}

// MARK: - Incidents

private struct IncidentRow: View {
    let incident: SystemIncident

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                Text(title)
                    .font(Theme.Typography.heading)
                    .foregroundStyle(Theme.Colors.primaryText)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: Theme.Spacing.s)
                // Severity is only worth colour while the incident is still open. A resolved `critical` is
                // history — it asks nothing of the user, so it reads as history.
                StatusPill(incident.resolved ? "Resolved" : incident.severity.capitalized,
                           color: statusColor)
            }

            if let workload = incident.workload {
                Text(workload)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if !incident.reasons.isEmpty {
                Text(incident.reasons.joined(separator: " · "))
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let mitigation = incident.mitigation {
                Text("Mitigated: \(mitigation)")
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }

            // The measured values at the time — the evidence the record exists for.
            if !incident.evidence.isEmpty {
                Text(incident.evidence.joined(separator: "  ·  "))
                    .font(Theme.Typography.monoSmall)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }

            // An unreadable stamp costs this ONE line, never the row and never the section.
            if let createdAt = incident.createdAt {
                Text(createdAt.saliRelative)
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .saliCard()
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityText)
    }

    private var title: String {
        incident.kind.replacingOccurrences(of: "_", with: " ").capitalized
    }

    private var statusColor: Color {
        guard !incident.resolved else { return Theme.Colors.secondaryText }
        switch incident.severity.lowercased() {
        case "critical", "emergency": return Theme.Colors.danger
        case "high":                  return Theme.Colors.warn
        default:                      return Theme.Colors.secondaryText
        }
    }

    private var accessibilityText: String {
        var parts = [title, incident.resolved ? "resolved" : "\(incident.severity), unresolved"]
        if let workload = incident.workload { parts.append(workload) }
        parts.append(contentsOf: incident.reasons)
        if let mitigation = incident.mitigation { parts.append("Mitigation: \(mitigation)") }
        if let createdAt = incident.createdAt { parts.append(createdAt.saliRelative) }
        return parts.joined(separator: ". ")
    }
}

#Preview {
    NavigationStack {
        SystemView()
    }
    .environmentObject(AppState())
}
