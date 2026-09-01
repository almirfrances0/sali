import Foundation
import SwiftUI

// Learning — the central question is "what can Sali do now that it couldn't before?" (§17). This screen
// makes learning visible as capability: verified lessons on one side, capabilities that are actually being
// USED (use_count/last_used, not dead entries) on the other, and the pending behavior proposals where the
// user is the final authority (§42) before anything changes how Sali works.

// MARK: - Wire shapes not yet modeled in DomainModels.swift (decoded forgivingly — see report)

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

/// `GET /api/v1/learning/candidates` → `{ candidates: [...] }` — verified/promoted/supported lessons,
/// best evidence first (src/sali/learning/candidates.py: `active()`).
private struct LearningCandidatesResponse: Decodable { let candidates: [LearningCandidateItem]? }

private struct LearningCandidateItem: Decodable, Identifiable {
    let candidateId: String?
    let lesson: String?
    let scope: String?
    let evidenceLevel: Int?
    let confidence: Double?
    let timesSuccessful: Int?
    let timesFailed: Int?
    let verificationState: String?
    let updatedAt: Date?
    var id: String { candidateId ?? UUID().uuidString }
    enum CodingKeys: String, CodingKey {
        case candidateId = "id", lesson, scope, evidenceLevel = "evidence_level", confidence
        case timesSuccessful = "times_successful", timesFailed = "times_failed"
        case verificationState = "verification_state", updatedAt = "updated_at"
    }
}

/// `GET /api/v1/capabilities` → `{ capabilities: [Capability], counts: {...} }`.
private struct CapabilitiesResponse: Decodable {
    let capabilities: [Capability]?
    let counts: CapabilityCounts?
}

private struct CapabilityCounts: Decodable {
    let total: Int?
    let usable: Int?
}

/// `GET /api/v1/capabilities/overview` — self-describing: built-in tool groups merged with learned
/// capabilities (src/sali/runtime/cognitive.py: `capability_overview()`). Both kinds use the key
/// `capability` for their display name; only `builtin` groups carry `tools`, only `learned` items carry
/// `confidence`.
private struct CapabilityOverview: Decodable {
    let builtin: [CapabilityOverviewItem]?
    let learned: [CapabilityOverviewItem]?
}

private struct CapabilityOverviewItem: Decodable, Identifiable {
    let capability: String?
    let status: String?
    let tools: [String]?
    let confidence: Double?
    var id: String { capability ?? UUID().uuidString }
    var displayName: String { (capability ?? "capability").replacingOccurrences(of: "_", with: " ") }
}

/// `GET /api/v1/behavior/proposals` → `{ proposals: [...] }` (src/sali/learning/behavior.py: `pending()`).
private struct BehaviorProposalsResponse: Decodable { let proposals: [BehaviorProposalItem]? }

private struct BehaviorProposalItem: Decodable, Identifiable {
    let proposalId: String?
    let trigger: String?
    let proposedBehavior: String?
    let reason: String?
    let confidence: Double?
    let timesObserved: Int?
    let createdAt: Date?
    var id: String { proposalId ?? UUID().uuidString }
    enum CodingKeys: String, CodingKey {
        case proposalId = "id", trigger, proposedBehavior = "proposed_behavior", reason, confidence
        case timesObserved = "times_observed", createdAt = "created_at"
    }
}

// MARK: - View model

@MainActor
final class LearningViewModel: ObservableObject {
    @Published var state: Loadable<Void> = .idle
    private var candidateCounts: LearningCandidateCounts?
    private var behaviorCounts: BehaviorCounts?
    @Published var candidates: [LearningCandidateItem] = []
    @Published var capabilities: [Capability] = []
    private var capabilityCounts: CapabilityCounts?
    @Published var builtinGroups: [CapabilityOverviewItem] = []
    @Published var learnedOverview: [CapabilityOverviewItem] = []
    @Published var proposals: [BehaviorProposalItem] = []

    @Published var workingProposalId: String?
    @Published var actionError: String?

    var activeLearningCount: Int { candidateCounts?.active ?? 0 }
    var usableCapabilityCount: Int { capabilityCounts?.usable ?? capabilities.count }
    var pendingProposalCount: Int { behaviorCounts?.pending ?? proposals.count }
    var openContradictionCount: Int { candidateCounts?.openContradictions ?? 0 }

    func load(api: APIClient) async {
        state = .loading
        do {
            async let health: LearningHealth = api.get("learning")
            async let candidatesResp: LearningCandidatesResponse = api.get("learning/candidates")
            async let capsResp: CapabilitiesResponse = api.get("capabilities")
            async let overviewResp: CapabilityOverview = api.get("capabilities/overview")
            async let proposalsResp: BehaviorProposalsResponse = api.get("behavior/proposals")
            let (h, cand, caps, overview, props) =
                try await (health, candidatesResp, capsResp, overviewResp, proposalsResp)
            candidateCounts = h.candidates
            behaviorCounts = h.behavior
            candidates = cand.candidates ?? []
            capabilities = caps.capabilities ?? []
            capabilityCounts = caps.counts
            builtinGroups = overview.builtin ?? []
            learnedOverview = overview.learned ?? []
            proposals = props.proposals ?? []
            state = .loaded(())
        } catch {
            state = .failed((error as? APIError)?.errorDescription ?? "Couldn't reach Sali.")
        }
    }

    func approve(_ proposal: BehaviorProposalItem, api: APIClient) async {
        await act(on: proposal, path: "approve", api: api)
    }

    func reject(_ proposal: BehaviorProposalItem, api: APIClient) async {
        await act(on: proposal, path: "reject", api: api)
    }

    private func act(on proposal: BehaviorProposalItem, path: String, api: APIClient) async {
        guard let id = proposal.proposalId else { return }
        workingProposalId = id
        actionError = nil
        do {
            try await api.postVoid("behavior/proposals/\(id)/\(path)")
            proposals.removeAll { $0.id == proposal.id }
        } catch {
            actionError = (error as? APIError)?.errorDescription ?? "That didn't go through — try again."
        }
        workingProposalId = nil
    }
}

// MARK: - View

struct LearningView: View {
    @EnvironmentObject var appState: AppState
    @StateObject private var viewModel = LearningViewModel()

    // Pushed as a NavigationLink destination from MoreView, so — like NotificationsView — this does not
    // open its own NavigationStack; it shares the one already on screen.
    var body: some View {
        content
            .navigationTitle("Learning")
            .task { if case .idle = viewModel.state { await viewModel.load(api: appState.api) } }
            .refreshable { await viewModel.load(api: appState.api) }
    }

    @ViewBuilder private var content: some View {
        switch viewModel.state {
        case .idle, .loading:
            LoadingState("Reviewing what Sali has learned…")
        case .failed(let message):
            ErrorStateView(message) { Task { await viewModel.load(api: appState.api) } }
        case .loaded:
            List {
                headlineSection
                if !viewModel.proposals.isEmpty { proposalsSection }
                capabilitiesSection
                selfDescriptionSection
                lessonsSection
            }
            .listStyle(.insetGrouped)
            .alert("Couldn't update that", isPresented: Binding(
                get: { viewModel.actionError != nil },
                set: { if !$0 { viewModel.actionError = nil } }
            )) {
                Button("OK", role: .cancel) { viewModel.actionError = nil }
            } message: {
                Text(viewModel.actionError ?? "")
            }
        }
    }

    // MARK: sections

    private var headlineSection: some View {
        Section {
            VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                Text("What can Sali do now that it couldn't before?")
                    .font(Theme.Typography.heading)
                HStack(spacing: Theme.Spacing.l) {
                    metric(viewModel.usableCapabilityCount, "Capabilities")
                    metric(viewModel.activeLearningCount, "Verified lessons")
                    metric(viewModel.pendingProposalCount, "Awaiting you",
                          tint: viewModel.pendingProposalCount > 0 ? Theme.Colors.accent : nil)
                }
                if viewModel.openContradictionCount > 0 {
                    Label("\(viewModel.openContradictionCount) open contradiction(s) in what Sali has learned",
                         systemImage: "exclamationmark.triangle")
                        .font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.warn)
                }
            }
            .padding(.vertical, Theme.Spacing.xs)
        }
    }

    private func metric(_ value: Int, _ label: String, tint: Color? = nil) -> some View {
        VStack(spacing: Theme.Spacing.xs) {
            Text("\(value)").font(Theme.Typography.title).foregroundStyle(tint ?? Theme.Colors.primaryText)
            Text(label).font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
        }
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .combine)
    }

    private var proposalsSection: some View {
        Section {
            ForEach(viewModel.proposals) { proposal in
                ProposalRow(proposal: proposal, isWorking: viewModel.workingProposalId == proposal.id,
                           canDecide: appState.role.canControl) {
                    Task { await viewModel.approve(proposal, api: appState.api) }
                } onReject: {
                    Task { await viewModel.reject(proposal, api: appState.api) }
                }
            }
        } header: {
            Text("Waiting on you")
        } footer: {
            Text("Sali noticed a pattern worth adopting, but changes to how it behaves are never automatic — you decide.")
        }
    }

    private var capabilitiesSection: some View {
        Section {
            if viewModel.capabilities.isEmpty {
                Text("No learned capabilities yet — they appear once Sali picks up and verifies a new skill.")
                    .font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
            } else {
                ForEach(viewModel.capabilities) { capability in
                    CapabilityRow(capability: capability)
                }
            }
        } header: {
            Text("Capabilities")
        } footer: {
            Text("Use counts show these are actually being reached for — not shelved once learned.")
        }
    }

    private var selfDescriptionSection: some View {
        Section("How Sali describes itself") {
            if !viewModel.builtinGroups.isEmpty {
                VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                    Text("Built in").font(Theme.Typography.caption.weight(.semibold))
                        .foregroundStyle(Theme.Colors.secondaryText)
                    FlowChips(items: viewModel.builtinGroups.map(\.displayName))
                }
                .padding(.vertical, Theme.Spacing.xs)
            }
            if !viewModel.learnedOverview.isEmpty {
                VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                    Text("Learned").font(Theme.Typography.caption.weight(.semibold))
                        .foregroundStyle(Theme.Colors.secondaryText)
                    FlowChips(items: viewModel.learnedOverview.map(\.displayName))
                }
                .padding(.vertical, Theme.Spacing.xs)
            }
            if viewModel.builtinGroups.isEmpty && viewModel.learnedOverview.isEmpty {
                Text("Self-description isn't available right now.")
                    .font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
            }
        }
    }

    private var lessonsSection: some View {
        Section {
            if viewModel.candidates.isEmpty {
                Text("No verified lessons yet.")
                    .font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
            } else {
                ForEach(viewModel.candidates) { candidate in
                    LessonRow(candidate: candidate)
                }
            }
        } header: {
            Text("Recent lessons")
        } footer: {
            Text("Only evidence-backed, verified lessons appear here — an untested idea never becomes a fact.")
        }
    }
}

// MARK: - Rows

private struct CapabilityRow: View {
    let capability: Capability

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack {
                Text(capability.name).font(Theme.Typography.body.weight(.medium))
                Spacer()
                StatusPill(capability.status ?? "known", color: color(forStatus: capability.status))
            }
            if let purpose = capability.purpose {
                Text(purpose).font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
            }
            HStack(spacing: Theme.Spacing.m) {
                if let count = capability.useCount {
                    Label("\(count) use\(count == 1 ? "" : "s")", systemImage: "bolt.fill")
                        .font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
                }
                if let lastUsed = capability.lastUsed {
                    Label("used \(lastUsed.saliRelative)", systemImage: "clock")
                        .font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
                } else {
                    Label("not used yet", systemImage: "clock")
                        .font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
                }
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }

    private func color(forStatus status: String?) -> Color {
        switch status {
        case "verified", "available": Theme.Colors.ok
        case "degraded", "deprecated": Theme.Colors.warn
        default: Theme.Colors.info
        }
    }
}

private struct LessonRow: View {
    let candidate: LearningCandidateItem

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            Text(candidate.lesson ?? "A lesson Sali recorded")
                .font(Theme.Typography.body)
                .lineLimit(3)
            HStack(spacing: Theme.Spacing.m) {
                if let confidence = candidate.confidence {
                    ConfidenceBar(confidence).frame(width: 60)
                }
                if let state = candidate.verificationState {
                    StatusPill(state, color: state == "promoted" ? Theme.Colors.ok : Theme.Colors.info)
                }
                Spacer()
                if let updated = candidate.updatedAt {
                    Text(updated.saliRelative).font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.secondaryText)
                }
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }
}

private struct ProposalRow: View {
    let proposal: BehaviorProposalItem
    let isWorking: Bool
    let canDecide: Bool
    let onApprove: () -> Void
    let onReject: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            Text(proposal.proposedBehavior ?? "A behavior change Sali is proposing")
                .font(Theme.Typography.body.weight(.medium))
            if let reason = proposal.reason {
                Text(reason).font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
            }
            if let trigger = proposal.trigger {
                Label(trigger, systemImage: "sparkle")
                    .font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
            }
            if isWorking {
                ProgressView().frame(maxWidth: .infinity, alignment: .center)
            } else if canDecide {
                HStack {
                    Button {
                        onReject()
                    } label: {
                        Label("Not now", systemImage: "hand.raised").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)

                    Button {
                        onApprove()
                    } label: {
                        Label("Approve", systemImage: "checkmark").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(Theme.Colors.accent)
                }
                .padding(.top, Theme.Spacing.xs)
            } else {
                Text("Only the owner or a controller can decide on this.")
                    .font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
    }
}

/// A simple wrapping chip layout — no third-party dependency, just SwiftUI `Layout` (iOS 16+).
private struct FlowChips: View {
    let items: [String]

    var body: some View {
        FlowLayout(spacing: Theme.Spacing.s) {
            ForEach(items, id: \.self) { item in
                Text(item)
                    .font(Theme.Typography.caption)
                    .padding(.horizontal, Theme.Spacing.s)
                    .padding(.vertical, Theme.Spacing.xs)
                    .background(Theme.Colors.accentSoft)
                    .foregroundStyle(Theme.Colors.accent)
                    .clipShape(Capsule())
            }
        }
    }
}

private struct FlowLayout: Layout {
    var spacing: CGFloat

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x + size.width > maxWidth, x > 0 {
                x = 0; y += rowHeight + spacing; rowHeight = 0
            }
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
        return CGSize(width: maxWidth.isFinite ? maxWidth : x, height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let maxWidth = bounds.width
        var x: CGFloat = bounds.minX, y: CGFloat = bounds.minY, rowHeight: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x + size.width > bounds.minX + maxWidth, x > bounds.minX {
                x = bounds.minX; y += rowHeight + spacing; rowHeight = 0
            }
            subview.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}
