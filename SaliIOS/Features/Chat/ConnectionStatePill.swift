import SwiftUI

/// Small pill for the chat header. Draws connection state in the app's monochrome `SaliGlyph` alphabet —
/// the answers people actually need are "am I live?" and "over what path?" Details (host, latency,
/// attempt count) live in the diagnostics sheet that opens on tap.
///
/// It reuses `ConnectionBadge` rather than drawing its own dot: the header already carries a presence
/// glyph, and a second indicator in a saturated hue beside it broke the pure-ink identity and spoke a
/// different alphabet. Now both read as one family — same ring, same ink ramp, same tracked label.
public struct ConnectionStatePill: View {
    @ObservedObject var manager: ConnectionManager
    @State private var showDiagnostics = false

    public init(manager: ConnectionManager) {
        self.manager = manager
    }

    public var body: some View {
        Button {
            Haptics.light()
            showDiagnostics = true
        } label: {
            ConnectionBadge(manager.state)
                .padding(.horizontal, Theme.Spacing.s)
                .padding(.vertical, Theme.Spacing.xs)
                .background(Capsule(style: .continuous).fill(Theme.Colors.accentSoft))
                // A 44pt hit target around a deliberately small capsule — the visible pill stays compact,
                // the tap area clears the accessibility floor the rest of the app holds.
                .frame(minHeight: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Connection: \(manager.state.shortLabel). Tap for details.")
        .sheet(isPresented: $showDiagnostics) {
            ConnectionDiagnosticsView(manager: manager)
        }
    }
}

/// Diagnostics sheet — the details a curious/debugging user wants to see. Nested ObservableObject
/// children (`discovery`, `pathObserver`) are observed EXPLICITLY here so their updates repaint
/// the sheet without going through `manager.state`. SwiftUI doesn't propagate nested
/// objectWillChange through the parent's publisher, so a naked `manager.discovery.services` read
/// would silently miss updates.
public struct ConnectionDiagnosticsView: View {
    @ObservedObject var manager: ConnectionManager
    @ObservedObject var discovery: BonjourDiscovery
    @ObservedObject var pathObserver: NetworkPathObserver
    @Environment(\.dismiss) private var dismiss

    public init(manager: ConnectionManager) {
        self.manager = manager
        self.discovery = manager.discovery
        self.pathObserver = manager.pathObserver
    }

    public var body: some View {
        NavigationStack {
            List {
                Section("Current") {
                    LabeledContent("Mode", value: manager.state.shortLabel)
                    if let mode = manager.state.mode {
                        LabeledContent("Transport", value: mode.rawValue.capitalized)
                    }
                    if let identity = manager.lastIdentity {
                        LabeledContent("Runtime ID", value: String(identity.runtimeId.prefix(8)))
                        if let version = identity.version, !version.isEmpty {
                            LabeledContent("Version", value: version)
                        }
                    }
                    if case .localConnected(let host, let port, let latency) = manager.state {
                        LabeledContent("Endpoint", value: "\(host):\(port)")
                        if let latency {
                            LabeledContent("Probe latency", value: latencyText(latency))
                        }
                    }
                    if case .remoteConnected(let latency) = manager.state, let latency {
                        LabeledContent("Probe latency", value: latencyText(latency))
                    }
                    if let err = manager.lastError {
                        Text(err)
                            .font(.footnote.monospaced())
                            .foregroundStyle(.secondary)
                    }
                }

                Section("Last known") {
                    if let local = manager.lastKnownGoodLocal {
                        LabeledContent("Local", value: "\(local.host):\(local.port)")
                        if let rid = local.runtimeId {
                            LabeledContent("Local runtime ID", value: String(rid.prefix(8)))
                        }
                    } else {
                        Text("No local Sali found yet")
                            .foregroundStyle(.secondary)
                    }
                    if let remote = manager.lastKnownGoodRemote {
                        LabeledContent("Remote", value: remote.baseURL.absoluteString)
                    }
                }

                Section("Network") {
                    LabeledContent("Reachability", value: pathObserver.current.reach.rawValue.capitalized)
                    LabeledContent("Expensive", value: pathObserver.current.isExpensive ? "Yes" : "No")
                }

                Section("Discovery") {
                    if discovery.permissionDenied {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Local Network permission denied")
                                .font(.body.weight(.medium))
                            Text("The app can only reach Sali over the Internet. To enable direct Wi-Fi discovery, allow Local Network in iOS Settings → Sali.")
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                        }
                    } else {
                        LabeledContent("Browsing", value: discovery.isBrowsing ? "Yes" : "No")
                        LabeledContent("Services visible", value: "\(discovery.services.count)")
                    }
                    ForEach(Array(discovery.services.values), id: \.name) { service in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(service.name).font(.footnote.monospaced())
                            Text("\(service.host):\(service.port)")
                                .font(.caption.monospaced())
                                .foregroundStyle(.secondary)
                        }
                    }
                }

                Section("Transport preference") {
                    if manager.forcedRemote {
                        Text("Locked to Remote — the app will not use LAN discovery until you tap Auto-detect.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                        Button("Auto-detect (prefer LAN)") { manager.autoDetect() }
                    } else {
                        Text("Auto: the app prefers LAN when your PC is on the same Wi-Fi; otherwise it uses Remote.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                        Button("Force Remote") { manager.forceRemote() }
                    }
                }

                Section("Troubleshooting") {
                    // Two escape hatches when LAN discovery isn't kicking in on-network:
                    // 1. Refresh: restart the Bonjour browser (permission-race / cellular→wifi
                    //    transition can leave it silently stopped).
                    // 2. Reset trust: clear the enrolled runtime_id anchor — needed when the
                    //    anchor was stamped against a previous Sali (DB restore, migration,
                    //    session-file wipe on the PC).
                    Text("If Sali is on the same Wi-Fi but the pill stays Remote, try Refresh. If the PC's Sali was reset/restored, tap Reset local trust.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    Button("Refresh local discovery") { manager.refreshDiscovery() }
                    Button("Reset local trust anchor") { manager.resetLocalTrust() }
                }
            }
            .navigationTitle("Connection")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }

    private func latencyText(_ latency: TimeInterval) -> String {
        String(format: "%.0f ms", latency * 1000)
    }
}
