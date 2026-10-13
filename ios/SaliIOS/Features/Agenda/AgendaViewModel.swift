import Foundation
import SwiftUI

/// Owns the state for the agenda screen. One shape, one fetch: the backend synthesiser at
/// `GET /api/v1/agenda` returns the full ordered picture across tasks, commitments, schedules,
/// goals and initiatives. This VM only decodes and republishes — no client-side aggregation,
/// because the phone and the model must see the exact same view of what matters right now.
@MainActor
final class AgendaViewModel: ObservableObject {
    @Published private(set) var state: Loadable<AgendaResponse> = .idle
    @Published private(set) var staleReason: String?

    /// Event types whose arrival plausibly changes what /agenda would return. Anything else
    /// (chat streaming, tool progress, log noise) is filtered out so a busy conversation does
    /// not turn this screen into a request storm.
    static let liveTriggers: Set<EventType> = [
        .taskStarted, .taskProgress, .taskWaiting, .taskCompleted,
        .taskSuspended, .taskResumed, .intentRevoked,
        .other(.taskCancelled), .other(.taskSuperseded),
        .other(.goalCreated), .other(.goalUpdated), .other(.goalCompleted), .other(.goalBlocked),
        .other(.initiativeCreated), .other(.initiativeSelected),
        .other(.initiativeCompleted), .other(.initiativeBlocked), .other(.initiativeDeferred),
        .other(.obligationCreated), .other(.obligationResolved), .other(.obligationReopened),
        .other(.commitmentCreated), .other(.commitmentFulfilled),
        .other(.commitmentCancelled), .other(.commitmentOverdue),
    ]

    private var autoRefresh: Task<Void, Never>?
    private var debounce: Task<Void, Never>?

    // A monotonically increasing counter guards against overlap between the four call sites
    // (initial .task, .refreshable, the 30s auto-refresh, and the 250ms event debounce).
    // Only the most-recently-issued request is allowed to commit a payload; an older in-flight
    // response resuming after a newer one has already landed is silently dropped, so the UI
    // never reverts to stale data when two requests return out of order.
    private var loadSeq: Int = 0

    func load(api: APIClient) async {
        loadSeq &+= 1
        let seq = loadSeq
        if state.value == nil { state = .loading }
        do {
            let response: AgendaResponse = try await api.get("agenda")
            guard seq == loadSeq else { return }
            state = .loaded(response)
            staleReason = nil
        } catch {
            guard seq == loadSeq else { return }
            let message = (error as? APIError)?.errorDescription ?? "Couldn't load the agenda."
            if state.value == nil { state = .failed(message) } else { staleReason = message }
        }
    }

    func handle(event: SaliEvent, api: APIClient) {
        guard Self.liveTriggers.contains(event.type) else { return }
        debounce?.cancel()
        debounce = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(250))
            guard !Task.isCancelled, let self else { return }
            await self.load(api: api)
        }
    }

    // 30 s foreground refresh. A structured Task, not a Foundation Timer, so cancellation is
    // deterministic — the view starts it on appear + scene-active and stops it on both
    // disappear and scene-inactive, so a backgrounded app doesn't quietly keep hitting the API.
    func startAutoRefresh(api: APIClient) {
        stopAutoRefresh()
        autoRefresh = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(30))
                guard !Task.isCancelled, let self else { return }
                await self.load(api: api)
            }
        }
    }

    func stopAutoRefresh() {
        autoRefresh?.cancel(); autoRefresh = nil
    }

    deinit { autoRefresh?.cancel(); debounce?.cancel() }
}
