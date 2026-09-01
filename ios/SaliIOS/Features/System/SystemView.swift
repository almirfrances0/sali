import SwiftUI
import Charts

/// Drives `SystemView`: loads `GET /api/v1/system` + `/resource-incidents`, keeps a bounded rolling buffer
/// of VRAM%/GPU% samples for the trend chart, and reacts to live `resource.*` events. A transient refresh
/// failure never blanks the screen — the last known-good reading stays up (§16 "keep last-known, retry").
@MainActor
final class SystemViewModel: ObservableObject {
    struct ResourcePoint: Identifiable {
        let id = UUID()
        let at: Date
        let vramPct: Double?
        let gpuPct: Double?
    }

    @Published private(set) var health: Loadable<SystemHealth> = .idle
    @Published private(set) var lastKnownGood: SystemHealth?
    @Published private(set) var incidents: [ResourceIncident] = []
    @Published private(set) var incidentsErrorMessage: String?
    @Published private(set) var history: [ResourcePoint] = []

    /// Only the samples with a real VRAM reading — never plot a fabricated zero for an unread metric.
    var vramSeries: [ResourcePoint] { history.filter { $0.vramPct != nil } }

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
        if lastKnownGood == nil { health = .loading }
        await refresh()
        await refreshIncidents()
    }

    func refresh() async {
        guard let api else { return }
        do {
            let latest: SystemHealth = try await api.get("system")
            health = .loaded(latest)
            lastKnownGood = latest
            appendSample(latest.resources)
        } catch {
            if lastKnownGood == nil { health = .failed(error.localizedDescription) }
            // else: keep showing the last known-good reading; a transient blip shouldn't blank the dashboard
        }
    }

    func refreshIncidents() async {
        guard let api else { return }
        do {
            let response: IncidentsResponse = try await api.get("resource-incidents")
            incidents = response.incidents
            incidentsErrorMessage = nil
        } catch {
            incidentsErrorMessage = error.localizedDescription
        }
    }

    func handleLiveEvent(_ event: SaliEvent) {
        guard event.type == .resourceIncident || event.type == .resourceState else { return }
        Task { await refresh(); await refreshIncidents() }
    }

    private func appendSample(_ resources: ResourceSnapshot?) {
        guard let resources, resources.state != nil else { return }
        history.append(ResourcePoint(at: Date(), vramPct: resources.vramUsedPct, gpuPct: resources.gpuUtilPct))
        if history.count > maxHistory { history.removeFirst(history.count - maxHistory) }
    }
}

private struct IncidentsResponse: Decodable { let incidents: [ResourceIncident] }

/// System Health (§18/§19) — one consolidated, real-time view of the host Sali runs on, tied directly to
/// Prompt 12's resource-stewardship ladder: the state banner, gauges, and incidents here are the same
/// deterministic signals that drive `ResourceAuthority` server-side (never a second, invented model of
/// "how Sali is doing"). Pushed as a `NavigationLink` destination from `MoreView` (see `RootView.swift`),
/// so — like `NotificationsView` — this does not open its own `NavigationStack`.
struct SystemView: View {
    @EnvironmentObject private var appState: AppState
    @StateObject private var viewModel = SystemViewModel()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Group {
            switch viewModel.health {
            case .idle, .loading:
                LoadingState("Reading the machine…")
            case .failed(let message):
                // `refresh()` only ever sets `.failed` when there's no last known-good reading to fall
                // back on — once one succeeds, a later transient error keeps showing `content` instead.
                ErrorStateView(message) { Task { await viewModel.refresh() } }
            case .loaded:
                content
            }
        }
        .navigationTitle("System")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) { ConnectionBadge(appState.ws.state) }
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

    // MARK: - Loaded content

    private var content: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Spacing.xl) {
                if let health = viewModel.lastKnownGood {
                    StateBanner(resources: health.resources, reduceMotion: reduceMotion)
                    metricsGrid(health.resources)
                    modelRow(health)
                    SubsystemRow(health: health.health)
                }
                historySection
                incidentsSection
            }
            .padding(Theme.Spacing.l)
        }
        .background(Theme.Colors.background)
    }

    private func metricsGrid(_ resources: ResourceSnapshot?) -> some View {
        let columns = [GridItem(.flexible(), spacing: Theme.Spacing.m), GridItem(.flexible(), spacing: Theme.Spacing.m)]
        return LazyVGrid(columns: columns, spacing: Theme.Spacing.m) {
            MetricCard(title: "GPU", systemImage: "cpu", valueText: Self.fmtPct(resources?.gpuUtilPct),
                       detailText: nil, fraction: Self.fraction(resources?.gpuUtilPct), tint: Theme.Colors.info)
            MetricCard(title: "VRAM", systemImage: "memorychip", valueText: Self.fmtPct(resources?.vramUsedPct),
                       detailText: Self.vramDetail(resources), fraction: Self.fraction(resources?.vramUsedPct),
                       tint: Self.tint(resources?.vramUsedPct, warn: GaugeThresholds.vramWarn, danger: GaugeThresholds.vramDanger))
            MetricCard(title: "RAM", systemImage: "memorychip.fill", valueText: Self.fmtPct(resources?.ramUsedPct),
                       detailText: nil, fraction: Self.fraction(resources?.ramUsedPct),
                       tint: Self.tint(resources?.ramUsedPct, warn: GaugeThresholds.ramWarn, danger: GaugeThresholds.ramDanger))
            MetricCard(title: "CPU load", systemImage: "gauge.with.needle", valueText: Self.fmtPct(resources?.cpuLoad),
                       detailText: "per core", fraction: Self.fraction(resources?.cpuLoad, cap: GaugeThresholds.loadDangerPct),
                       tint: Self.tint(resources?.cpuLoad, warn: GaugeThresholds.loadWarnPct, danger: GaugeThresholds.loadDangerPct))
            MetricCard(title: "Disk", systemImage: "internaldrive", valueText: Self.fmtPct(resources?.diskUsedPct),
                       detailText: nil, fraction: Self.fraction(resources?.diskUsedPct),
                       tint: Self.tint(resources?.diskUsedPct, warn: GaugeThresholds.diskWarn, danger: GaugeThresholds.diskDanger))
            MetricCard(title: "Temperature", systemImage: "thermometer.medium", valueText: Self.fmtTemp(resources?.temperatureC),
                       detailText: nil, fraction: Self.fraction(resources?.temperatureC, cap: 100),
                       tint: Self.tint(resources?.temperatureC, warn: GaugeThresholds.tempWarnC, danger: GaugeThresholds.tempDangerC))
        }
    }

    private func modelRow(_ health: SystemHealth) -> some View {
        HStack {
            Label(health.model, systemImage: "brain")
                .font(Theme.Typography.body)
                .lineLimit(1)
            Spacer()
            Label("\(health.websocketConnections)", systemImage: "antenna.radiowaves.left.and.right")
                .font(Theme.Typography.caption)
                .foregroundStyle(Theme.Colors.secondaryText)
                .accessibilityLabel("\(health.websocketConnections) live connections")
        }
        .saliCard()
    }

    private var historySection: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            SectionHeader("Recent VRAM trend", subtitle: "Last \(viewModel.vramSeries.count) samples")
            if viewModel.vramSeries.count >= 2 {
                VRAMChart(points: viewModel.vramSeries)
                    .frame(height: 140)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(chartAccessibilityLabel)
                if let latest = viewModel.history.last {
                    Text("Now — VRAM \(Self.fmtPct(latest.vramPct)) · GPU \(Self.fmtPct(latest.gpuPct))")
                        .font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.secondaryText)
                }
            } else {
                Text("Gathering samples — check back in a moment.")
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .frame(maxWidth: .infinity, minHeight: 60, alignment: .center)
            }
        }
        .saliCard()
    }

    private var chartAccessibilityLabel: String {
        let values = viewModel.vramSeries.compactMap(\.vramPct)
        guard let last = values.last else { return "VRAM usage trend, no data yet." }
        let first = values.first ?? last
        let direction = last > first ? "rising" : (last < first ? "falling" : "steady")
        return "VRAM usage trend over \(values.count) samples, \(direction), most recently \(Int(last.rounded()))%."
    }

    private var incidentsSection: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            SectionHeader("Incidents", subtitle: "Host-endangering events Sali has recorded")
            if let message = viewModel.incidentsErrorMessage, viewModel.incidents.isEmpty {
                ErrorStateView(message) { Task { await viewModel.refreshIncidents() } }
                    .frame(minHeight: 100)
            } else if viewModel.incidents.isEmpty {
                Label("No incidents recorded.", systemImage: "checkmark.shield")
                    .font(Theme.Typography.body)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .padding(.vertical, Theme.Spacing.s)
            } else {
                VStack(spacing: Theme.Spacing.s) {
                    ForEach(viewModel.incidents) { incident in
                        IncidentRow(incident: incident)
                    }
                }
            }
        }
    }

    // MARK: - Formatting & thresholds (display-only; the authoritative state is `resources.state`)

    private static func fmtPct(_ value: Double?) -> String {
        guard let value else { return "—" }
        return "\(Int(value.rounded()))%"
    }
    private static func fmtTemp(_ value: Double?) -> String {
        guard let value else { return "—" }
        return "\(Int(value.rounded()))°C"
    }
    private static func vramDetail(_ resources: ResourceSnapshot?) -> String? {
        guard let used = resources?.vramUsedMB, let total = resources?.vramTotalMB, total > 0 else { return nil }
        return "\(Int(used)) / \(Int(total)) MB"
    }
    /// A 0...1 fraction for the gauge bar. `cap` lets a metric that can exceed 100% (CPU load, temperature)
    /// still render a sensible bar instead of clipping silently.
    private static func fraction(_ pct: Double?, cap: Double = 100) -> Double? {
        guard let pct else { return nil }
        return max(0, min(1, pct / cap))
    }
    private static func tint(_ value: Double?, warn: Double, danger: Double) -> Color {
        guard let value else { return Theme.Colors.idle }
        if value >= danger { return Theme.Colors.danger }
        if value >= warn { return Theme.Colors.warn }
        return Theme.Colors.ok
    }
}

/// Mirrors `ResourceBudget` defaults in `sali/runtime/resources.py` — used only to tint the gauges
/// consistently with the same thresholds the runtime itself classifies against. The state banner's color
/// still comes authoritatively from `resources.state`, never from these local thresholds.
private enum GaugeThresholds {
    static let vramWarn = 85.0, vramDanger = 92.0
    static let ramWarn = 85.0, ramDanger = 93.0
    static let diskWarn = 90.0, diskDanger = 96.0
    static let tempWarnC = 82.0, tempDangerC = 87.0
    static let loadWarnPct = 200.0, loadDangerPct = 400.0
}

// MARK: - State banner

private struct StateBanner: View {
    let resources: ResourceSnapshot?
    let reduceMotion: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack(spacing: Theme.Spacing.s) {
                Image(systemName: icon)
                    .font(.system(size: 20, weight: .semibold))
                    .accessibilityHidden(true)
                Text(title)
                    .font(Theme.Typography.heading)
                Spacer()
                if resources?.preservation == true {
                    StatusPill("Preservation mode active", color: Theme.Colors.danger)
                }
            }
            Text(meaning)
                .font(Theme.Typography.body)
                .foregroundStyle(Theme.Colors.secondaryText)
                .fixedSize(horizontal: false, vertical: true)
        }
        .foregroundStyle(color)
        .padding(Theme.Spacing.l)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(color.opacity(0.12))
        .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.l, style: .continuous))
        .animation(reduceMotion ? nil : Theme.Motion.gentle, value: resources?.state)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(title). \(meaning).\(resources?.preservation == true ? " Preservation mode active." : "")")
    }

    private var known: Bool { resources?.state != nil }
    private var level: ResourceLevel { resources?.resourceState ?? .safe }

    private var title: String { known ? level.rawValue.capitalized : "State unknown" }
    private var meaning: String {
        known ? level.meaning : "Sali couldn't read host resources just now — this will refresh automatically."
    }
    private var color: Color {
        guard known else { return Theme.Colors.idle }
        switch level {
        case .safe: return Theme.Colors.ok
        case .elevated: return Theme.Colors.info
        case .high: return Theme.Colors.warn
        case .critical, .emergency: return Theme.Colors.danger
        }
    }
    private var icon: String {
        guard known else { return "questionmark.circle.fill" }
        switch level {
        case .safe: return "checkmark.circle.fill"
        case .elevated: return "chart.line.uptrend.xyaxis"
        case .high: return "exclamationmark.triangle.fill"
        case .critical: return "shield.lefthalf.filled"
        case .emergency: return "exclamationmark.octagon.fill"
        }
    }
}

// MARK: - Metric card

private struct MetricCard: View {
    let title: String
    let systemImage: String
    let valueText: String
    let detailText: String?
    let fraction: Double?      // nil = unavailable
    let tint: Color

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            Label(title, systemImage: systemImage)
                .font(Theme.Typography.caption)
                .foregroundStyle(Theme.Colors.secondaryText)
            Text(valueText)
                .font(Theme.Typography.title)
                .foregroundStyle(Theme.Colors.primaryText)
            if let detailText {
                Text(detailText)
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }
            GaugeBar(fraction: fraction, tint: tint)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .saliCard()
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(title): \(valueText)\(detailText.map { ", \($0)" } ?? "")")
    }
}

private struct GaugeBar: View {
    let fraction: Double?
    let tint: Color
    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Theme.Colors.surfaceRaised)
                if let fraction {
                    Capsule()
                        .fill(tint)
                        .frame(width: max(4, CGFloat(fraction) * geo.size.width))
                }
            }
        }
        .frame(height: 6)
        .accessibilityHidden(true)
    }
}

// MARK: - Subsystem health

private struct SubsystemRow: View {
    let health: SubsystemHealth?
    var body: some View {
        HStack(spacing: Theme.Spacing.s) {
            pill("Datastore", health?.datastore)
            pill("Model", health?.model)
            pill("Embedder", health?.embedder)
            pill("Internet", health?.internet)
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .combine)
    }
    private func pill(_ name: String, _ ok: Bool?) -> some View {
        StatusPill(name, color: ok == true ? Theme.Colors.ok : (ok == false ? Theme.Colors.danger : Theme.Colors.idle))
    }
}

// MARK: - Chart

private struct VRAMChart: View {
    let points: [SystemViewModel.ResourcePoint]

    var body: some View {
        Chart(points) { point in
            let value = point.vramPct ?? 0
            AreaMark(x: .value("Time", point.at), y: .value("VRAM %", value))
                .foregroundStyle(Theme.Colors.accent.opacity(0.16))
                .interpolationMethod(.catmullRom)
            LineMark(x: .value("Time", point.at), y: .value("VRAM %", value))
                .foregroundStyle(Theme.Colors.accent)
                .interpolationMethod(.catmullRom)
                .lineStyle(StrokeStyle(lineWidth: 2))
        }
        .chartYScale(domain: 0...100)
        .chartXAxis(.hidden)
        .chartYAxis {
            AxisMarks(position: .leading, values: [0, 50, 100]) { value in
                AxisGridLine()
                AxisValueLabel {
                    if let intValue = value.as(Int.self) { Text("\(intValue)%") }
                }
            }
        }
    }
}

// MARK: - Incidents

private struct IncidentRow: View {
    let incident: ResourceIncident

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            HStack(alignment: .top) {
                Text(incident.kind.replacingOccurrences(of: "_", with: " ").capitalized)
                    .font(Theme.Typography.heading)
                Spacer()
                StatusPill(incident.severity.capitalized, color: severityColor)
            }
            HStack(spacing: Theme.Spacing.s) {
                StatusPill(incident.resolved == true ? "Resolved" : "Active",
                           color: incident.resolved == true ? Theme.Colors.ok : Theme.Colors.warn)
                if let createdAt = incident.createdAt {
                    Text(createdAt, style: .relative)
                        .font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.secondaryText)
                }
            }
            if let workload = incident.workload, !workload.isEmpty {
                Label(workload, systemImage: "gearshape")
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .lineLimit(1)
            }
            if let mitigation = incident.mitigation, !mitigation.isEmpty {
                Text(mitigation)
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .saliCard()
        .accessibilityElement(children: .combine)
    }

    private var severityColor: Color {
        switch incident.severity.lowercased() {
        case "critical", "emergency": Theme.Colors.danger
        case "high", "warning": Theme.Colors.warn
        default: Theme.Colors.info
        }
    }
}

#Preview {
    let appState = AppState()
    return NavigationStack {
        SystemView()
    }
    .environmentObject(appState)
}
