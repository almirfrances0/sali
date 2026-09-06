import SwiftUI
import UIKit
import UniformTypeIdentifiers
import PhotosUI

/// The primary screen (Prompt 13, §5/§7) — Sali's single shared conversation, rendered as a **document**
/// rather than a wall of boxes. History loads once over REST; the live turn streams in from the WebSocket.
///
/// Two rules hold the whole transcript together, and everything below is downstream of them:
///
/// 1. **A container means "you said it"; unboxed flow means "Sali."** Sali's replies are the one content
///    type that emits headings, tables and fenced code, so putting them in a bubble produced a
///    box-in-a-box-in-a-box in the *narrowest* column on screen. Sali now writes into the page: no
///    background, no border, just a 36pt gutter holding the mark. Your own words get the container — a
///    quiet raised card with one asymmetric corner, not a high-contrast ink slab.
/// 2. **One work line, one ambient loop.** A single `TurnStatusLine` is pinned at the top of the live turn
///    (`phase · elapsed`) for the whole turn, so nothing reflows the prose as tools start and stop, and the
///    only ambient motion on screen is the breathing `SaliMark`. The line reports; the composer acts —
///    Stop lives there alone, in one fixed place that never scrolls away mid-reply.
public struct ChatView: View {
    @EnvironmentObject private var appState: AppState

    public init() {}

    public var body: some View {
        ChatContentView(viewModel: appState.chatViewModel)
    }
}

// MARK: - Layout

/// The transcript's geometry, in one place. Chat is the only screen where the *column* is the design, so
/// every inset here is named and shared rather than re-guessed per bubble.
private enum ChatLayout {
    /// The screen margin — the same `Spacing.screenMargin` every other surface in the app uses. Chat used
    /// to be the lone screen at 12, which is why its content never lined up with anything else.
    static let margin: CGFloat = Theme.Spacing.screenMargin         // 16
    /// The column that carries Sali's mark. Stays *reserved* when the mark is collapsed away in a group,
    /// so grouped turns keep the same left edge as the turn that opened the group.
    static let markColumn: CGFloat = 28
    /// Mark column + its gap: the single number Sali's prose is inset by, and the exact amount code blocks
    /// and tables bleed back out by.
    static let gutter: CGFloat = markColumn + Theme.Spacing.s       // 36
    /// The minimum air to the left of one of your own messages.
    static let userMinGap: CGFloat = 56
    /// The largest share of the column one of your messages may occupy. Enforced through the leading
    /// `Spacer`'s `minLength` (not a `maxWidth` frame) so a short message still hugs its own text.
    static let userWidthFraction: CGFloat = 0.78
    /// The mark's diameter inside `markColumn`.
    static let markSize: CGFloat = 26
    /// The live turn's status row: a real 44pt control band at the top of the turn.
    static let statusRowHeight: CGFloat = 44
    /// The concentric radius for anything nested inside a user turn's container: the card is `Radius.l`
    /// and its padding is `Spacing.s`, so its contents must curve by exactly the difference, or the two
    /// curves fight along the diagonal.
    static let nestedRadius: CGFloat = Theme.Radius.nested(in: Theme.Radius.l, inset: Theme.Spacing.s)
}

/// Reads the transcript's real width so "78% of the column" is a measurement, not a guess.
private struct ChatWidthKey: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

private struct ChatContentView: View {
    @EnvironmentObject private var appState: AppState
    @ObservedObject var viewModel: ChatViewModel

    @State private var draft = ""
    @State private var isAtBottom = true
    /// Whether the live reply should keep the transcript pinned to the newest text as Sali types. Distinct
    /// from `isAtBottom` on purpose: during streaming the content grows faster than a single scroll settles,
    /// so the geometry flag flickers to "not at bottom" for a frame — gating tail-follow on THAT is exactly
    /// why auto-scroll used to stop mid-reply. This intent only releases when the reader deliberately pulls
    /// well up (hysteresis in `BottomProximity`), and re-engages the moment they return to the tail or a new
    /// turn starts. So: Sali's typing scrolls down on its own, unless you scrolled up to read back.
    @State private var stickToBottom = true
    @State private var showDocumentPicker = false
    @State private var showPhotoPicker = false
    @State private var photoItem: PhotosPickerItem?
    @State private var previewImage: PreviewImage?
    /// The photo OR file chosen but not yet sent. Staged in the composer so you can say what you want done
    /// with it and send both as one turn.
    @State private var pending: PendingAttachment?
    /// True while a picked photo's bytes are still loading — the strip shows a placeholder rather than
    /// appearing to do nothing.
    @State private var isPreparingAttachment = false
    /// The transcript's measured width — the basis for the user column's 78% cap.
    @State private var transcriptWidth: CGFloat = 0
    /// Whether the transcript has already been placed at the newest turn for this presentation. Set once
    /// so the position is asserted when the transcript arrives and NEVER again while the reader is in it.
    /// See `pinToNewest` for why the position has to be asserted at all.
    @State private var didPinToNewest = false
    /// The in-flight settling passes, so a fresh load supersedes an older one instead of racing it.
    @State private var pinTask: Task<Void, Never>?
    @FocusState private var inputFocused: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    private let bottomAnchorID = "chat.bottom"
    private let liveTurnFallbackID = "chat.live.turn"

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                // ONE strip at the top, in every load state — identity and current state together. It
                // replaces the navigation bar outright (see `ChatHeader`), so nothing is drawn above it.
                // A small connection pill sits at the top-trailing corner: it answers "am I live?
                // over what path?" and opens a diagnostics sheet on tap. This is the only surface
                // that reflects the ConnectionManager state (LAN vs Remote vs offline), so wiring
                // it here makes the LAN-discovery feature actually visible.
                ChatHeader(state: headerState, isLiveTurnOnScreen: isLive)
                    .overlay(alignment: .topTrailing) {
                        ConnectionStatePill(manager: appState.connection)
                            .padding(.trailing, 12)
                            .padding(.top, 6)
                    }

                Group {
                    switch viewModel.loadState {
                    case .idle, .loading:
                        if viewModel.messages.isEmpty {
                            LoadingState("Loading your conversation…")
                        } else {
                            chatBody
                        }
                    case .failed(let message):
                        if viewModel.messages.isEmpty {
                            ErrorStateView(message) { Task { await viewModel.load(api: appState.api) } }
                        } else {
                            chatBody
                        }
                    case .loaded:
                        chatBody
                    }
                }
            }
            .background(Theme.Colors.background)
            // The header IS the bar. A system navigation bar above it would be a second, emptier strip
            // saying the same word — exactly the stacking this screen is being rid of.
            .toolbar(.hidden, for: .navigationBar)
        }
        .task {
            viewModel.configure(api: appState.api)
            if case .idle = viewModel.loadState {
                await viewModel.load(api: appState.api)
            }
            await viewModel.refreshActiveTask(api: appState.api)
        }
        // Heal a live turn that froze because the socket dropped mid-reply (a lock, a backgrounding, a
        // blip): when a reconnect finishes its replay catch-up, re-fetch the durable transcript so the
        // frozen half-bubble becomes the complete persisted reply — no relaunch needed. Token deltas are
        // ephemeral and never replayed, so this REST reconcile is the only thing that can close the gap;
        // it mirrors what TasksView/TaskDetailView already do on this exact signal.
        .onChange(of: appState.replayCaughtUpTick) { _, _ in
            Task { await viewModel.reconcileAfterReconnect(api: appState.api) }
        }
        // Neutral title: this surface also carries download and attachment failures, not just sends.
        .alert("Something went wrong", isPresented: Binding(
            get: { viewModel.sendError != nil },
            set: { if !$0 { viewModel.sendError = nil } }
        )) {
            Button("OK", role: .cancel) { viewModel.sendError = nil }
        } message: {
            Text(viewModel.sendError ?? "")
        }
        .sheet(isPresented: $showDocumentPicker) {
            DocumentPicker { url in handlePickedFile(url) }
        }
        .fullScreenCover(item: $previewImage) { preview in
            FullScreenImageViewer(image: preview.image) { previewImage = nil }
        }
        .photosPicker(isPresented: $showPhotoPicker, selection: $photoItem, matching: .images)
        .onChange(of: photoItem) { _, item in
            // Picking a photo ATTACHES it to the composer — it must not send on its own. You write your
            // message with the picture in front of you, then send both together (one message, one turn).
            guard let item else { return }
            // `loadTransferable` can take seconds for an iCloud photo. Show the placeholder immediately;
            // a picker that dismisses to nothing reads as broken.
            withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick)) {
                isPreparingAttachment = true
            }
            Task {
                defer { photoItem = nil }
                guard let data = try? await item.loadTransferable(type: Data.self) else {
                    withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick)) {
                        isPreparingAttachment = false
                    }
                    viewModel.sendError = "Couldn't load that photo. Try picking it again."
                    return
                }
                let (jpeg, name) = downscaleForUpload(data)
                guard let ui = UIImage(data: jpeg) else {
                    withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick)) {
                        isPreparingAttachment = false
                    }
                    viewModel.sendError = "That photo couldn't be read."
                    return
                }
                withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick)) {
                    isPreparingAttachment = false
                    pending = PendingAttachment(data: jpeg, filename: name, image: ui)
                }
                inputFocused = true
            }
        }
    }

    /// Downscale a picked image to a sane upload size (long edge ≤ 1600, JPEG 0.8). Falls back to the
    /// original bytes if it can't be decoded.
    private func downscaleForUpload(_ data: Data) -> (Data, String) {
        let name = "photo-\(UUID().uuidString.prefix(8)).jpg"
        guard let image = UIImage(data: data) else { return (data, name) }
        let maxEdge: CGFloat = 1600
        let scale = min(1, maxEdge / max(image.size.width, image.size.height))
        let target = CGSize(width: image.size.width * scale, height: image.size.height * scale)
        let renderer = UIGraphicsImageRenderer(size: target)
        let resized = renderer.image { _ in image.draw(in: CGRect(origin: .zero, size: target)) }
        return (resized.jpegData(compressionQuality: 0.8) ?? data, name)
    }

    private func handlePickedFile(_ url: URL) {
        let didAccess = url.startAccessingSecurityScopedResource()
        defer { if didAccess { url.stopAccessingSecurityScopedResource() } }
        // Stat the file size FIRST — do not read the file into memory before we know it fits.
        // Previously we called `Data(contentsOf: url)` unconditionally, so a 4 K video picked
        // from Files (200 MB - 4 GB) would be fully read into RAM on the main actor before the
        // 50 MB guard could fire, jetsam-killing the app before the alert ever showed.
        let fileSize: Int
        do {
            let values = try url.resourceValues(forKeys: [.fileSizeKey])
            fileSize = values.fileSize ?? Int.max
        } catch {
            viewModel.sendError = "Couldn't read that file."
            return
        }
        guard fileSize <= 50 * 1024 * 1024 else {
            viewModel.sendError = "That file is over the 50 MB limit."
            return
        }
        guard let data = try? Data(contentsOf: url) else {
            viewModel.sendError = "Couldn't read that file."; return
        }
        // Staged, not sent: the attach menu behaves exactly the same for a photo and a file — it waits in
        // the composer until you send it, with or without a message.
        withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick)) {
            pending = PendingAttachment(data: data, filename: url.lastPathComponent, image: nil)
        }
        inputFocused = true
    }

    // MARK: - Body states

    private var chatBody: some View {
        // Tap anywhere in the CONTENT area to put the keyboard away — the empty/first-run screen included.
        // Deliberately excludes the input bar, so tapping the field still focuses it. `simultaneousGesture`
        // so it never swallows a turn's own tap (opening a photo, retrying a failed send).
        Group {
            if viewModel.messages.isEmpty && viewModel.streaming == nil && !viewModel.isSending {
                chatEmptyState
            } else {
                transcript
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .contentShape(Rectangle())
        .simultaneousGesture(TapGesture().onEnded {
            if inputFocused { inputFocused = false }
        })
        .background(Theme.Colors.background)
        // THE composer is a bottom SAFE-AREA INSET, not a VStack sibling. That is what lets the keyboard
        // lift it cleanly and float it just above the keys, while the transcript's scroll region is inset
        // by exactly the composer's height — so the last message AND the caret line stay visible while
        // typing, instead of the field sitting flush against the keyboard with the text hidden behind it.
        // No `.toolbar(.keyboard)` "Done" bar any more: it added a SECOND strip between the field and the
        // keys — the crowding Almir felt — and a top-tier chat doesn't use one. Dismissal is the
        // interactive scroll-down (`.scrollDismissesKeyboard(.interactively)`) and the tap-to-dismiss above.
        .safeAreaInset(edge: .bottom, spacing: 0) {
            inputBar
        }
    }

    private var chatEmptyState: some View {
        VStack(spacing: Theme.Spacing.xl) {
            SaliMark(size: 60)
            VStack(spacing: Theme.Spacing.s) {
                Text("Talk with Sali")
                    .font(Theme.Typography.title)
                    .foregroundStyle(Theme.Colors.primaryText)
                Text("Ask anything. Sali keeps the context of your day and coordinates your tools.")
                    .font(Theme.Typography.callout)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, Theme.Spacing.xl)
        .padding(.vertical, Theme.Spacing.heroGap)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - Transcript

    /// The column available to content, inside the screen margins.
    private var contentWidth: CGFloat { max(0, transcriptWidth - ChatLayout.margin * 2) }

    /// The air to the left of your own messages. It is *this* — not a `maxWidth` frame — that caps the user
    /// column at 78%: a flexible frame would stretch a two-word message to the full cap, while a floor on
    /// the leading `Spacer` caps the long ones and lets the short ones hug.
    private var userGap: CGFloat {
        max(ChatLayout.userMinGap, contentWidth * (1 - ChatLayout.userWidthFraction))
    }

    /// The same cap, stated as a width. The `Spacer` floor alone can't hold a string with no break
    /// opportunity in it — a long URL or an absolute path reports one enormous ideal width and drags the
    /// bubble past the column with it. `nil` until the transcript has actually been measured, so the first
    /// frame never lays a bubble out at zero width.
    private var userMaxWidth: CGFloat? {
        contentWidth > 0 ? max(120, contentWidth - userGap) : nil
    }

    private var rows: [TranscriptRow] { TranscriptRow.build(from: viewModel.messages) }

    /// True while there is a turn to report on — the one condition that puts the live turn on screen.
    private var isLive: Bool { viewModel.isSending || viewModel.streaming != nil }

    /// The single phase word for the live turn. Words, not motion: the tool name IS the information, so a
    /// running tool outranks "Writing", and the generic placeholder collapses to "Thinking".
    private var livePhase: TurnPhase {
        if viewModel.isQueued { return .queued }
        let activity = (viewModel.activity ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !activity.isEmpty, activity != "Thinking…", activity != "Thinking" {
            return .working(activity)
        }
        if let text = viewModel.streaming?.text, !text.isEmpty { return .writing }
        return .thinking
    }

    /// Air above the live turn — the same grouping rhythm the finalized turns use.
    private var liveGapAbove: CGFloat {
        guard let last = viewModel.messages.last else { return 0 }
        // Same three-step rhythm `TranscriptRow.build` applies, so the live turn doesn't sit at a spacing
        // no finalized turn ever uses: a proactive message opens a chapter, you open a block, Sali
        // continuing itself is one item.
        if last.role == .agent { return Theme.Spacing.sectionGap }
        return last.role == .user ? Theme.Spacing.blockGap : Theme.Spacing.itemGap
    }

    // MARK: - Header state

    /// What the header says right now, resolved in ONE place from state that already exists — the socket,
    /// `AppState.presence`, the shared `/cognitive` snapshot, and this view model's own live turn. Nothing
    /// here is fetched, inferred, or filled in: every branch names the signal it came from, and a branch
    /// with no real detail to add says nothing rather than something plausible.
    ///
    /// The order is the order of authority:
    ///
    /// 1. **The socket.** If we are not live we do not know what Sali is doing, and every state below
    ///    would be a stale claim — so the header becomes the connection notice. (This is the whole of the
    ///    old offline banner: one strip, not two.)
    /// 2. **This conversation's own turn.** It is the thing on screen; it outranks anything happening
    ///    elsewhere.
    /// 3. **Sali's own state**, from the shared presence value and the one snapshot behind it.
    ///
    /// **The status WORD is never written here.** It is read off `SaliPresence.label` (and, for the two
    /// socket states, `ConnectionState.shortLabel`), because a hand-typed word is how the header came to
    /// say "Thinking" and "Waiting on you" while the chip two screens away said "REPLYING" and "NEEDS
    /// YOU" about the identical state. Taking the string from the same place the chip does means they
    /// cannot drift again.
    private var headerState: ChatHeaderState {
        let connection = appState.ws.state
        guard connection.isLive else {
            switch connection {
            case .connecting:
                return ChatHeaderState(glyph: .connecting, status: connection.shortLabel, detail: nil)
            case .reconnecting(let attempt):
                return ChatHeaderState(glyph: .connecting, status: connection.shortLabel,
                                       detail: attempt > 1 ? "attempt \(attempt)" : nil)
            case .offline:
                return ChatHeaderState(glyph: .offline, status: SaliPresence.offline.label,
                                       detail: "connection interrupted")
            case .idle, .connected:
                return ChatHeaderState(glyph: .offline, status: SaliPresence.offline.label,
                                       detail: "not connected")
            }
        }

        if isLive {
            switch livePhase {
            case .queued:
                return ChatHeaderState(glyph: .queued, status: "Queued", detail: "Sali is busy")
            case .thinking:
                // Turn started, nothing streamed yet. The state IS `.thinking` presence, so it takes that
                // state's word — "Replying" — and the detail carries the only thing that distinguishes
                // this moment from the next one.
                return ChatHeaderState(glyph: .replying, status: SaliPresence.thinking.label,
                                       detail: "starting")
            case .writing:
                return ChatHeaderState(glyph: .replying, status: SaliPresence.thinking.label, detail: nil)
            case .working(let summary):
                return ChatHeaderState(glyph: .working, status: SaliPresence.working.label, detail: summary)
            }
        }

        let snapshot = appState.snapshot.value
        let presence = appState.presence
        switch presence {
        case .waitingOnYou:
            return ChatHeaderState(glyph: presence.glyph, status: presence.label,
                                   detail: snapshot?.attentionReason)
        case .blocked:
            return ChatHeaderState(glyph: presence.glyph, status: presence.label,
                                   detail: snapshot?.attentionReason)
        case .working:
            return ChatHeaderState(glyph: presence.glyph, status: presence.label,
                                   detail: snapshot?.headline)
        case .thinking:
            return ChatHeaderState(glyph: presence.glyph, status: presence.label, detail: nil)
        case .idle:
            return ChatHeaderState(glyph: presence.glyph, status: presence.label, detail: nextWakeupDetail)
        case .offline:
            return ChatHeaderState(glyph: presence.glyph, status: presence.label, detail: nil)
        }
    }

    /// The one thing that is genuinely true about an idle Sali: when it next wakes itself up. Only shown
    /// while it is still in the future — a wake-up time that has passed says nothing.
    private var nextWakeupDetail: String? {
        guard let wake = appState.snapshot.value?.nextWakeup, wake > Date() else { return nil }
        return "wakes \(wake.formatted(date: .omitted, time: .shortened))"
    }

    // MARK: - Transcript view

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(rows) { row in
                        VStack(alignment: .leading, spacing: 0) {
                            if let spine = row.spine {
                                TimeSpine(label: spine)
                                    .padding(.bottom, Theme.Spacing.blockGap)
                            }
                            TurnView(
                                message: row.message,
                                showsMark: row.showsMark,
                                userGap: userGap,
                                userMaxWidth: userMaxWidth,
                                downloadedURL: row.message.attachment?.artifactId
                                    .flatMap { viewModel.downloadedFiles[$0] },
                                isDownloading: row.message.attachment?.artifactId != nil
                                    && row.message.attachment?.artifactId == viewModel.downloadingArtifactId,
                                onDownloadFile: {
                                    if let att = row.message.attachment {
                                        Task { await viewModel.downloadAttachment(att, api: appState.api) }
                                    }
                                },
                                onRetry: { Task { await viewModel.retry(row.message, api: appState.api) } },
                                onOpenImage: { ui in previewImage = PreviewImage(image: ui) },
                                onRetryImage: { Task { await viewModel.retryImage(row.message, api: appState.api) } },
                                onRetryFile: { Task { await viewModel.retryFile(row.message, api: appState.api) } }
                            )
                        }
                        .padding(.top, row.gapAbove)
                        .id(row.id)
                    }

                    if isLive {
                        LiveTurnView(
                            text: viewModel.streaming?.text ?? "",
                            phase: livePhase,
                            startedAt: viewModel.turnStartedAt,
                            steps: viewModel.activitySteps
                        )
                        .padding(.top, liveGapAbove)
                        // NAMESPACED so the live bubble can never share SwiftUI identity with the settled
                        // row it becomes. `finalizeStreaming` deliberately KEEPS the streaming bubble's id
                        // when the turn lands in `messages`, so this used to publish `.id(X)` here while the
                        // ForEach above simultaneously published `.id(X)` for the finalized row. SwiftUI
                        // matched them as the same view and REUSED this LiveTurnView instead of building the
                        // settled TurnView — so the bubble kept showing the last partial token, kept spinning
                        // "Writing", and never rendered the file card (LiveTurnView draws no attachment).
                        // Scrolling the row out and back, or relaunching, forced a rebuild and everything
                        // "appeared" — the exact symptom. The data was always correct; only the view was stale.
                        .id("live:\(viewModel.streaming?.id ?? liveTurnFallbackID)")
                    }

                    // Bottom anchor for `scrollTo`. It is NOT the source of truth for "am I at the
                    // bottom" any more — see `BottomProximity` below.
                    Color.clear
                        .frame(height: 4)
                        .id(bottomAnchorID)
                        .modifier(LegacyBottomSentinel(isAtBottom: $isAtBottom))
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, ChatLayout.margin)
                .padding(.top, Theme.Spacing.blockGap)
                .padding(.bottom, Theme.Spacing.l)
            }
            // Open on the newest message. The old code only scrolled on a messages.count change, which
            // fires before the LazyVStack has laid out, so the scroll landed nowhere and the transcript
            // sat at the very FIRST message. iOS 17 anchors the initial content offset natively.
            .defaultScrollAnchor(.bottom)
            .scrollDismissesKeyboard(.interactively)
            // Real geometry, not a lazily-laid-out sentinel. It also self-heals the initial pin: when the
            // content height moves under a reader who is at the tail (a late image decode, re-parsed
            // markdown, a lazily-realized row), re-assert the bottom so the viewport can't be left stranded
            // past the content end — the black-screen-until-you-scroll-up bug.
            .modifier(BottomProximity(isAtBottom: $isAtBottom, stickToBottom: $stickToBottom,
                                      onTailHeightChange: {
                var t = Transaction(); t.disablesAnimations = true
                withTransaction(t) { proxy.scrollTo(bottomAnchorID, anchor: .bottom) }
            }))
            .background {
                GeometryReader { geo in
                    Color.clear.preference(key: ChatWidthKey.self, value: geo.size.width)
                }
            }
            .onPreferenceChange(ChatWidthKey.self) { transcriptWidth = $0 }
            .overlay(alignment: .bottomTrailing) { jumpToLatest(proxy: proxy) }
            .onChange(of: viewModel.messages.count) { _, _ in
                // Follow new messages only if the reader is already at the bottom — never yank them up
                // from history they've scrolled back to read.
                if isAtBottom { scrollToBottom(proxy: proxy) }
            }
            .onChange(of: viewModel.streaming?.text) { _, _ in
                // Follow Sali's typing on its OWN — gated on the sticky intent, not the flickering
                // `isAtBottom`, so lag can't stop it. Fires ~15×/s, so it uses `followTail` (a fast linear
                // nudge), never a spring — 15 overlapping springs a second is where the judder came from.
                if stickToBottom { followTail(proxy: proxy) }
            }
            .onChange(of: viewModel.isSending) { _, isSending in
                // The user just sent — re-arm the follow and jump to the tail so they watch the reply come in.
                if isSending { stickToBottom = true; scrollToBottom(proxy: proxy) }
            }
            .onChange(of: inputFocused) { _, focused in
                if focused { scrollToBottom(proxy: proxy) }
            }
            // The transcript is being shown: place it at the newest turn. This is the path that covers a
            // cold start (the ScrollView is built in the very update that delivers history) AND coming
            // back to the tab with everything already loaded.
            .onAppear { pinToNewest(proxy) }
            // A load or reload replaced the whole transcript — the previous position described rows that
            // no longer exist, so this one is placed from scratch.
            .onChange(of: viewModel.historyGeneration) { _, _ in pinToNewest(proxy, force: true) }
            .onDisappear {
                pinTask?.cancel()
                // Left at the tail? Then arrive at the tail next time — the transcript may have grown
                // while this screen was away. Left in the middle of history? That was a place the reader
                // chose, and it is kept: coming back must not throw them to the bottom.
                if isAtBottom { didPinToNewest = false }
            }
        }
    }

    /// Place the transcript on the newest turn — the one thing `.defaultScrollAnchor(.bottom)` cannot be
    /// trusted to do here.
    ///
    /// **The bug it fixes.** `defaultScrollAnchor` resolves exactly once, against the content size known
    /// at the first layout pass. At that instant the transcript is a `LazyVStack` whose rows have not
    /// been measured: rows below the fold do not exist yet, `MarkdownText` re-parses in its own
    /// `onAppear` (so every bubble's height is re-resolved a pass later), and `ImageAttachmentView`
    /// swaps a fixed 200pt placeholder for the real image inside a `task`. The content size therefore
    /// keeps moving *underneath a content offset that was already fixed*, and the viewport ends up parked
    /// past the end of the transcript — a screen that looks empty until you drag up and the messages
    /// appear above you.
    ///
    /// Nothing recovered from it, either. `onChange(of: messages.count)` cannot fire for history that
    /// landed in the same update that created this ScrollView (the switch in `body` builds the transcript
    /// at the instant `loadState` flips to `.loaded`), `streaming`/`isSending` are nil and false on a cold
    /// open, and `BottomProximity` reports `isAtBottom == true` while parked past the end — so even the
    /// "Jump to latest" escape hatch stayed hidden.
    ///
    /// So the position is asserted rather than hinted: scroll to the bottom anchor with animation
    /// explicitly disabled, and repeat across a few layout ticks so the last pass lands after the content
    /// size has stopped moving. Every pass is the same idempotent assertion, so re-running is free.
    ///
    /// It runs **once per presentation** (`didPinToNewest`), or on an explicit `force` when a load
    /// replaced the transcript wholesale. It can never yank a reader who has scrolled back into history.
    private func pinToNewest(_ proxy: ScrollViewProxy, force: Bool = false) {
        guard force || !didPinToNewest else { return }
        guard !viewModel.messages.isEmpty || isLive else { return }
        didPinToNewest = true
        pinTask?.cancel()
        pinTask = Task { @MainActor in
            for delay in Self.pinSettleDelays {
                if delay > 0 {
                    try? await Task.sleep(nanoseconds: delay)
                    if Task.isCancelled { return }
                }
                var transaction = Transaction()
                transaction.disablesAnimations = true      // arriving somewhere is not a movement
                withTransaction(transaction) {
                    proxy.scrollTo(bottomAnchorID, anchor: .bottom)
                }
            }
        }
    }

    /// This frame, the next runloop turn, ~4 frames in, ~15 frames in, and one late pass at ~0.55s. The
    /// early ticks catch the lazy rows and re-parsed markdown; the late one catches a slow image decode on
    /// iOS 17, where `BottomProximity` has no scroll-geometry self-heal and this window is the ONLY thing
    /// that can keep the viewport from parking past a still-growing transcript. Kept short of a second so a
    /// reader who immediately scrolls up still owns the position.
    private static let pinSettleDelays: [UInt64] = [0, 16_000_000, 70_000_000, 250_000_000, 550_000_000]

    /// Discrete jump — a new turn, a send, the keyboard opening, or the jump-to-latest button.
    private func scrollToBottom(proxy: ScrollViewProxy) {
        withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.standard)) {
            proxy.scrollTo(bottomAnchorID, anchor: .bottom)
        }
    }

    /// Continuous tail-follow while tokens arrive — near-instant and linear, so consecutive follows blend
    /// instead of fighting each other.
    private func followTail(proxy: ScrollViewProxy) {
        withAnimation(reduceMotion ? nil : .linear(duration: 0.08)) {
            proxy.scrollTo(bottomAnchorID, anchor: .bottom)
        }
    }

    /// Shown only when the reader has scrolled away from the tail — the state was already tracked, there
    /// was simply no way to act on it.
    @ViewBuilder
    private func jumpToLatest(proxy: ScrollViewProxy) -> some View {
        Group {
            if !isAtBottom {
                Button { scrollToBottom(proxy: proxy) } label: {
                    Image(systemName: "chevron.down")
                        .font(Theme.Typography.footnote.weight(.semibold))
                        .foregroundStyle(Theme.Colors.primaryText)
                        .frame(width: 38, height: 38)
                        .background(Theme.Colors.surfaceRaised, in: Circle())
                        .overlay(Circle().strokeBorder(Theme.Colors.border, lineWidth: Theme.Stroke.hairline))
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .padding(.trailing, Theme.Spacing.s)
                .padding(.bottom, Theme.Spacing.s)
                .transition(reduceMotion ? .opacity : .scale.combined(with: .opacity))
                .accessibilityLabel("Jump to latest")
            }
        }
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick), value: isAtBottom)
    }

    // MARK: - Composer

    private var isBusy: Bool { viewModel.isSending || viewModel.streaming != nil }

    /// The field says what is true: a message sent while Sali is working is delivered and queued behind the
    /// current turn (§10, one cognition slot), never dropped and never silently held.
    private var fieldPrompt: String {
        isBusy ? "Message Sali — sends after this turn" : "Message Sali…"
    }

    private var inputBar: some View {
        VStack(spacing: 0) {
            Rectangle()
                .fill(Theme.Colors.separator)
                .frame(height: Theme.Stroke.hairline)

            pendingAttachmentStrip

            HStack(alignment: .bottom, spacing: Theme.Spacing.xs) {
                attachMenu

                composerField

                // Stop and Send are INDEPENDENT controls, never two faces of one slot. Sharing a slot
                // meant attaching a photo mid-stream hid Send (nothing left to press) and typing while
                // Sali streamed vanished Stop. Both are reachable at the same time — and Stop stays here,
                // always on screen, even when the live turn's own status line has scrolled out of view.
                if isBusy {
                    stopButton
                }
                sendButton
            }
            .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle), value: pending)
            .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle), value: isPreparingAttachment)
            .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle), value: isBusy)
            .padding(.horizontal, Theme.Spacing.s)
            .padding(.top, Theme.Spacing.s)
            // A little more room beneath the field than above it, so the caret line clears the keyboard
            // with comfortable air rather than sitting right on its top edge.
            .padding(.bottom, Theme.Spacing.m)
        }
        // The bar is ONE surface, and it runs to the bottom of the screen. The fill used to sit on the
        // control row alone, so the separator, the attachment strip and the whole home-indicator inset
        // showed canvas: a composer that stopped 34pt short of the edge with a stripe of a different
        // colour beneath it. `ignoresSafeArea` on the BACKGROUND only, so nothing about the layout moves
        // and the keyboard still pushes the bar exactly as far as it should.
        .background(Theme.Colors.surface.ignoresSafeArea(.container, edges: .bottom))
    }

    /// The field grows line by line with what you type, up to a cap, and then scrolls inside itself. The
    /// cap is smaller at accessibility type sizes — six lines of 40pt text is the whole screen.
    private var composerLineCap: Int { dynamicTypeSize.isAccessibilitySize ? 3 : 6 }

    private var composerField: some View {
        // The placeholder is drawn rather than passed in: the system one is a fixed system grey, which is
        // the one colour in the composer that wasn't a Theme token, and it can't say anything about state.
        TextField("", text: $draft, axis: .vertical)
            .font(Theme.Typography.body)
            .foregroundStyle(Theme.Colors.primaryText)
            .tint(Theme.Colors.accent)
            .focused($inputFocused)
            .lineLimit(1...composerLineCap)
            .padding(.horizontal, Theme.Spacing.m)
            // 12 + one body line + 12 lands exactly on the 44pt control height, so a one-line field is
            // the same height as the Send target beside it and its text sits on the glyph's centre line.
            .padding(.vertical, Theme.Spacing.m)
            .frame(minHeight: 44)
            // An input well is the one place the ramp goes *down* — sunken inside the `surface`
            // composer bar, so the field reads as somewhere to put something.
            .background(Theme.Colors.surfaceSunken)
            .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.xl, style: .continuous))
            // The edge tracks FOCUS, not busy-ness. Dimming it while Sali worked implied the field was
            // disabled, which is the opposite of what happens: you can always type, and what you send
            // is queued behind the current turn.
            .saliHairline(radius: Theme.Radius.xl,
                          color: inputFocused ? Theme.Colors.borderStrong : Theme.Colors.border)
            .overlay(alignment: .leading) {
                if draft.isEmpty {
                    Text(fieldPrompt)
                        .font(Theme.Typography.body)
                        .foregroundStyle(Theme.Colors.tertiaryText)
                        .lineLimit(1)
                        .truncationMode(.tail)
                        .padding(.horizontal, Theme.Spacing.m)
                        .allowsHitTesting(false)
                        .accessibilityHidden(true)
                }
            }
            .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick), value: inputFocused)
            .accessibilityLabel("Message Sali")
            .accessibilityHint(isBusy
                               ? "Sali is working. Your message is delivered and answered after this turn."
                               : "")
            .onSubmit { submit() }
    }

    /// ONE attach control. Two adjacent icon buttons made the user classify their own content before the
    /// system did; a single menu asks the question once.
    private var attachMenu: some View {
        Menu {
            // The Photo option is shown ONLY when the active model can actually see images. Switch to a
            // text-only model (e.g. Qwen3-Coder) and it vanishes — you can't send an image to a model
            // that can't read it. Files stays (Sali reads text/code/PDF without vision).
            if appState.modelHasVision {
                Button { showPhotoPicker = true } label: {
                    Label("Photo Library", systemImage: "photo.on.rectangle")
                }
            }
            Button { showDocumentPicker = true } label: {
                Label("Files", systemImage: "folder")
            }
        } label: {
            Image(systemName: "plus")
                .font(Theme.Typography.titleSmall)
                .foregroundStyle(Theme.Colors.primaryText)
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
        }
        .accessibilityLabel("Attach a photo or file")
    }

    /// Interrupt the running turn. Available whenever there is something to interrupt.
    private var stopButton: some View {
        Button(action: {
            Haptics.firm()   // interrupting a running turn is a weighty state change — it should land
            viewModel.stop(api: appState.api)
        }) {
            ZStack {
                Circle()
                    .fill(Theme.Colors.surfaceRaised)
                    .frame(width: 32, height: 32)
                Circle()
                    .strokeBorder(Theme.Colors.border, lineWidth: Theme.Stroke.emphasis)
                    .frame(width: 32, height: 32)
                RoundedRectangle(cornerRadius: Theme.Radius.xxs, style: .continuous)
                    .fill(Theme.Colors.primaryText)
                    .frame(width: 11, height: 11)
            }
            .frame(width: 44, height: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Stop generation")
        .transition(reduceMotion ? .identity : .scale.combined(with: .opacity))
    }

    /// Always present so the composer never dead-ends; enabled the moment there is anything to send. Ink,
    /// because it is the one primary action on the screen.
    private var sendButton: some View {
        Button(action: submit) {
            // A fixed glyph size, deliberately: this is a control filling a fixed 44pt target, not type.
            // Scaling it with Dynamic Type would grow the glyph out of the target it has to sit inside.
            Image(systemName: "arrow.up.circle.fill")
                .font(.system(size: 30, weight: .semibold))
                // `placeholder` is a FILL token — at 0xE6E6EB on a 0xF9F9FB bar the disabled arrow was
                // very nearly invisible, which reads as "the button is gone", not "there is nothing to
                // send yet". `borderStrong` is unmistakably inactive and unmistakably still there.
                .foregroundStyle(canSubmit ? Theme.Colors.accent : Theme.Colors.borderStrong)
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick), value: canSubmit)
        .disabled(!canSubmit)
        .accessibilityLabel(isBusy ? "Send message — it will follow Sali's current turn" : "Send message")
    }

    /// The composed-but-unsent attachment — a photo or a document — shown above the field so you can see
    /// what you are writing about, plus a placeholder while a picked photo's bytes are still loading.
    @ViewBuilder private var pendingAttachmentStrip: some View {
        if isPreparingAttachment {
            attachmentStripRow(title: "Preparing…",
                               subtitle: "Loading it from your library.",
                               onRemove: nil) {
                RoundedRectangle(cornerRadius: Theme.Radius.s, style: .continuous)
                    .fill(Theme.Colors.surfaceSunken)
                    .frame(width: 44, height: 44)
                    .overlay(ProgressView().controlSize(.small))
            }
        } else if let attachment = pending {
            attachmentStripRow(title: attachment.isImage ? "Photo attached" : attachment.filename,
                               subtitle: "Add a message, or send it on its own.",
                               onRemove: {
                                   withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick)) {
                                       pending = nil
                                   }
                               }) {
                if let image = attachment.image {
                    Image(uiImage: image)
                        .resizable()
                        .scaledToFill()
                        .frame(width: 44, height: 44)
                        .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.s, style: .continuous))
                        .saliHairline(radius: Theme.Radius.s, color: Theme.Colors.border)
                } else {
                    RoundedRectangle(cornerRadius: Theme.Radius.s, style: .continuous)
                        .fill(Theme.Colors.surfaceSunken)
                        .frame(width: 44, height: 44)
                        .overlay(Image(systemName: saliFileIcon(for: attachment.filename))
                            .font(Theme.Typography.body)
                            .foregroundStyle(Theme.Colors.secondaryText))
                        .saliHairline(radius: Theme.Radius.s, color: Theme.Colors.border)
                }
            }
        }
    }

    private func attachmentStripRow<Leading: View>(
        title: String, subtitle: String, onRemove: (() -> Void)?,
        @ViewBuilder leading: () -> Leading
    ) -> some View {
        HStack(spacing: Theme.Spacing.s) {
            leading()
            VStack(alignment: .leading, spacing: 1) {
                Text(title)
                    .font(Theme.Typography.footnote.weight(.semibold))
                    .foregroundStyle(Theme.Colors.primaryText)
                    .lineLimit(1).truncationMode(.middle)
                Text(subtitle)
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)

            if let onRemove {
                Button(action: onRemove) {
                    Image(systemName: "xmark")
                        .font(Theme.Typography.caption.weight(.bold))
                        .foregroundStyle(Theme.Colors.secondaryText)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Remove attachment")
            }
        }
        .padding(.horizontal, Theme.Spacing.s)
        .padding(.top, Theme.Spacing.s)
        .transition(reduceMotion ? .identity : .move(edge: .bottom).combined(with: .opacity))
        .accessibilityElement(children: .combine)
    }

    private var draftIsEmpty: Bool { draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    // §10: you can always compose — a message sent while Sali is busy is delivered and queued, never dropped.
    // An attachment on its own is a complete message, so it can be sent with no text.
    private var canSubmit: Bool { !draftIsEmpty || pending != nil }

    private func submit() {
        guard canSubmit else { return }
        Haptics.light()   // the reply is going — the most-used control gets the same beat as the rest
        let text = draft
        let attachment = pending
        draft = ""
        pending = nil
        Task {
            if let attachment {
                if attachment.isImage {
                    await viewModel.sendImage(attachment.data, filename: attachment.filename,
                                              caption: text, api: appState.api)
                } else {
                    await viewModel.sendFile(filename: attachment.filename, data: attachment.data,
                                             caption: text, api: appState.api)
                }
            } else {
                await viewModel.send(text, api: appState.api)
            }
        }
    }
}

/// A photo or file chosen in the composer but NOT yet sent — held so a message can be written alongside it.
/// `image` non-nil ⇒ a photo (it carries the thumbnail); nil ⇒ a document.
private struct PendingAttachment: Identifiable, Equatable {
    let id = UUID()
    let data: Data
    let filename: String
    let image: UIImage?
    var isImage: Bool { image != nil }
    static func == (a: PendingAttachment, b: PendingAttachment) -> Bool { a.id == b.id }
}

/// Tracks "is the reader at the tail" from the scroll view's REAL geometry. The previous sentinel lived
/// inside a LazyVStack, so it only reported anything once it was nearly on screen — tail-following both
/// yanked the reader out of history and silently stopped working.
private struct BottomProximity: ViewModifier {
    @Binding var isAtBottom: Bool
    /// The tail-follow intent, with HYSTERESIS: re-engages within 80pt of the tail, releases only past
    /// 200pt. The gap is what makes streaming auto-scroll robust — a live reply grows a line at a time and
    /// the viewport is never more than a few points behind, so it never crosses 200pt from lag alone; only a
    /// deliberate scroll-up does. (Gating on the bare 80pt `atBottom` flapped, which is why follow stopped.)
    @Binding var stickToBottom: Bool
    /// Fired when the content HEIGHT changes while the reader is at the tail — a lazy row appeared, markdown
    /// re-parsed taller, an image finished decoding, the live turn grew. The caller re-pins to the bottom so
    /// the viewport can never be left parked past the (now taller) content end — the "chat looks empty until
    /// you scroll up" black screen. One observer reports every signal, so there is no ordering race. No-op on
    /// iOS 17 (no scroll geometry there); `pinToNewest`'s widened settle window is that path's cover.
    var onTailHeightChange: () -> Void = {}

    private struct Snapshot: Equatable { var atBottom: Bool; var farUp: Bool; var height: CGFloat }

    func body(content: Content) -> some View {
        if #available(iOS 18.0, *) {
            content.onScrollGeometryChange(for: Snapshot.self) { geo in
                let distanceFromBottom = geo.contentSize.height - (geo.contentOffset.y + geo.containerSize.height)
                return Snapshot(atBottom: distanceFromBottom <= 80,
                                farUp: distanceFromBottom > 200,
                                height: geo.contentSize.height)
            } action: { old, new in
                isAtBottom = new.atBottom
                if new.atBottom { stickToBottom = true }        // returned to the tail → follow again
                else if new.farUp { stickToBottom = false }     // deliberately pulled up → stop following
                if new.atBottom && new.height != old.height { onTailHeightChange() }
            }
        } else {
            content
        }
    }
}

/// The old sentinel, kept ONLY as the iOS 17 fallback where `onScrollGeometryChange` doesn't exist.
private struct LegacyBottomSentinel: ViewModifier {
    @Binding var isAtBottom: Bool

    func body(content: Content) -> some View {
        if #available(iOS 18.0, *) {
            content
        } else {
            content
                .onAppear { isAtBottom = true }
                .onDisappear { isAtBottom = false }
        }
    }
}

/// SF Symbol for a filename's extension — shared by the composer strip and the transcript's file card.
private func saliFileIcon(for filename: String) -> String {
    switch (filename as NSString).pathExtension.lowercased() {
    case "png", "jpg", "jpeg", "gif", "heic", "webp": return "photo"
    case "pdf": return "doc.richtext"
    case "zip", "gz", "tar": return "doc.zipper"
    case "csv", "xlsx", "xls": return "tablecells"
    case "md", "txt", "rtf": return "doc.text"
    case "swift", "py", "js", "ts", "json", "html", "css": return "chevron.left.forwardslash.chevron.right"
    default: return "doc"
    }
}

// MARK: - The header

/// What the header is saying, as data — so the *derivation* (`ChatContentView.headerState`) and the
/// *drawing* (`ChatHeader`) can be read, and argued with, separately.
private struct ChatHeaderState: Equatable {
    /// The state drawn as a SHAPE — literally the app's one alphabet (`SaliGlyph`), not a lookalike.
    /// This screen used to own a private copy of it, which is how the header ended up drawing a bare
    /// solid disc for a streaming reply while the toolbar chip drew a small breathing core inside a ring:
    /// the same state, two different pictures. It uses six of the eight letters plus the two only a
    /// socket-facing chat screen can be in — `connecting`, and a turn `queued` behind Sali's current work.
    let glyph: SaliGlyph
    /// The state in one or two words. Always present — the header never has nothing to say.
    let status: String
    /// What the state is *about*, when something real names it: the running tool, the objective, the
    /// question Sali is blocked on. `nil` is a common and correct answer; it is never filled with a
    /// plausible-sounding stand-in.
    let detail: String?

    /// The whole line as one sentence, so the truncated visual line loses nothing to VoiceOver.
    var spokenLabel: String {
        if let detail, !detail.isEmpty { return "Sali: \(status), \(detail)" }
        return "Sali: \(status)"
    }

    /// Whether this state is Sali doing something under its own power. Only these may animate.
    var isAmbient: Bool { glyph.isAmbient }
}

/// The top of the chat screen: Sali, and what Sali is doing, in ONE strip.
///
/// It replaces three competing things — a navigation bar whose entire content was the word "Sali", a
/// connection dot in the toolbar that described a *socket* rather than Sali, and an offline banner that
/// appeared and disappeared underneath them, shoving the transcript down by its own height every time the
/// network hiccuped. All three are folded in here, and the strip is the navigation bar (the system one is
/// hidden), so the screen gains a line of real information instead of a second row of chrome.
///
/// Three properties hold it together:
///
/// 1. **It cannot move.** Both lines are `lineLimit(1)`, so the height is a function of the type size and
///    nothing else: "Idle", "Working · rebuilding the index" and "Reconnecting · attempt 3" all occupy
///    exactly the same box. Losing the socket no longer reflows the transcript, because nothing appears —
///    a line of text changes.
/// 2. **It is on the transcript's own grid.** The mark sits in `ChatLayout.markColumn` at the screen
///    margin and the text starts at `ChatLayout.gutter`, which is the exact column Sali's prose is set
///    in. The header and the conversation share one left edge.
/// 3. **One ambient loop.** The glyph animates only for states Sali is actually driving, and holds its
///    resting frame while a live turn is on screen — that turn's mark is already the one moving thing.
private struct ChatHeader: View {
    let state: ChatHeaderState
    /// True while this conversation's own turn is rendered below, breathing its own mark.
    let isLiveTurnOnScreen: Bool

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// The trailing slot. One size for every state, so the glyph's optical centre never shifts as Sali
    /// changes — a status indicator that resizes as it changes reads as a layout bug. It scales with the
    /// status line it belongs to, and with nothing else.
    @ScaledMetric(relativeTo: .footnote) private var glyphSlot: CGFloat = 16

    var body: some View {
        HStack(alignment: .center, spacing: 0) {
            // The same mark, at the same size, in the same column as the marks in the transcript.
            SaliMark(size: ChatLayout.markSize)
                .frame(width: ChatLayout.markColumn, alignment: .leading)
                .padding(.trailing, Theme.Spacing.s)

            VStack(alignment: .leading, spacing: 0) {
                Text("Sali")
                    .font(Theme.Typography.heading)
                    .foregroundStyle(Theme.Colors.primaryText)
                    .lineLimit(1)
                statusLine
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel(state.spokenLabel)
            // The line is live: this is what makes VoiceOver re-read it as Sali's state moves, instead of
            // announcing it once on entry and going quiet for the rest of the turn.
            .accessibilityAddTraits(.updatesFrequently)

            Spacer(minLength: Theme.Spacing.s)

            // The same drawing the toolbar chip uses, from the same alphabet, at this screen's size.
            SaliStateGlyph(state.glyph,
                           animated: state.isAmbient && !isLiveTurnOnScreen && !reduceMotion,
                           side: glyphSlot * 0.8)
                .frame(width: glyphSlot, height: glyphSlot)
        }
        .padding(.horizontal, ChatLayout.margin)
        .padding(.vertical, Theme.Spacing.s)
        // A floor, not a height: the two lines already fix the size at any given type size, and this only
        // stops the strip collapsing under the mark at the smallest ones.
        .frame(minHeight: 52)
        .frame(maxWidth: .infinity)
        // The strip is one surface and it runs to the top of the screen, so the status bar sits on the
        // header rather than on a stripe of canvas above it. Background only — no layout moves.
        .background(Theme.Colors.surface.ignoresSafeArea(.container, edges: .top))
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(Theme.Colors.separator)
                .frame(height: Theme.Stroke.hairline)
        }
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick), value: state)
    }

    /// `state · what it is about`. The state word keeps its width (`layoutPriority`) and the detail is
    /// what gives way, so the header never truncates the half that carries the meaning.
    private var statusLine: some View {
        HStack(alignment: .firstTextBaseline, spacing: 0) {
            Text(state.status)
                .font(Theme.Typography.footnote.weight(.medium))
                .foregroundStyle(Theme.Colors.secondaryText)
                .layoutPriority(1)

            if let detail = state.detail, !detail.isEmpty {
                Text(verbatim: " · ")
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .layoutPriority(1)
                Text(detail)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.tertiaryText)
            }
        }
        .lineLimit(1)
        // One line always, so the strip's height is a function of the type size and nothing else. At
        // accessibility sizes the line SHRINKS to fit rather than truncating — a state you can't read is
        // the same as no state at all, and shrinking costs no height.
        .minimumScaleFactor(0.8)
        .truncationMode(.tail)
    }
}

// MARK: - Transcript rhythm

/// One row of the transcript: the turn, plus the *rhythm* around it — whether the mark repeats, how much
/// air sits above it, and whether a time spine opens a new chapter here.
///
/// Computed in one pass for the whole transcript so grouping and spacing are decided in a single place
/// instead of by each turn guessing about its neighbours.
private struct TranscriptRow: Identifiable {
    let id: String
    let message: ChatMessage
    /// False for a turn that continues the previous speaker's group — the mark is not repeated, but its
    /// column stays reserved so every line of a group shares one left edge.
    let showsMark: Bool
    let gapAbove: CGFloat
    /// A day or session divider to draw above this turn.
    let spine: String?

    /// A conversation that never ends still has a shape: a new day, or a long silence, starts a chapter.
    private static let sessionGap: TimeInterval = 60 * 60

    static func build(from messages: [ChatMessage]) -> [TranscriptRow] {
        var rows: [TranscriptRow] = []
        rows.reserveCapacity(messages.count)
        var previous: ChatMessage?

        for message in messages {
            let isAgent = message.role == .agent
            var spine: String?
            if let previous {
                if dayIndex(message.timestamp) != dayIndex(previous.timestamp) {
                    spine = dayLabel(for: message.timestamp)
                } else if message.timestamp.timeIntervalSince(previous.timestamp) >= sessionGap {
                    spine = message.timestamp.formatted(date: .omitted, time: .shortened)
                }
            } else {
                spine = dayLabel(for: message.timestamp)
            }

            let sameAuthor = previous.map { authorKey($0) == authorKey(message) } ?? false
            let gap: CGFloat
            if previous == nil {
                gap = 0
            } else if spine != nil || isAgent {
                gap = Theme.Spacing.sectionGap
            } else if sameAuthor {
                gap = Theme.Spacing.itemGap
            } else {
                gap = Theme.Spacing.blockGap
            }

            rows.append(TranscriptRow(id: message.id,
                                      message: message,
                                      showsMark: message.role != .user && (!sameAuthor || isAgent),
                                      gapAbove: gap,
                                      spine: spine))
            previous = message
        }
        return rows
    }

    /// Sali's own replies and Sali's proactive messages are different voices, so an agent turn always
    /// opens a new group even though both come from Sali.
    private static func authorKey(_ message: ChatMessage) -> String {
        switch message.role {
        case .user: "user"
        case .agent: "agent"
        case .assistant: "sali"
        }
    }

    /// Day bucket in the user's own time zone — cheap integer arithmetic, because this runs for every
    /// message on every transcript pass and `Calendar` comparisons are not free at 15 frames a second.
    private static func dayIndex(_ date: Date) -> Int {
        let offset = TimeInterval(TimeZone.current.secondsFromGMT(for: date))
        return Int(((date.timeIntervalSinceReferenceDate + offset) / 86_400).rounded(.down))
    }

    private static func dayLabel(for date: Date) -> String {
        if date.saliIsUnknownTime { return "Earlier" }
        switch dayIndex(Date()) - dayIndex(date) {
        case 0: return "Today"
        case 1: return "Yesterday"
        default: return date.formatted(.dateTime.weekday(.wide).day().month(.abbreviated))
        }
    }
}

/// The time spine — the quietest possible chapter break for a conversation with no beginning and no end.
private struct TimeSpine: View {
    let label: String

    var body: some View {
        HStack(spacing: Theme.Spacing.m) {
            rule
            Text(label)
                .font(Theme.Typography.metadata)
                .foregroundStyle(Theme.Colors.tertiaryText)
                .textCase(.uppercase)
                .tracking(0.6)
                .fixedSize()
            rule
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(label)
    }

    private var rule: some View {
        Rectangle().fill(Theme.Colors.separator).frame(height: Theme.Stroke.hairline)
    }
}

// MARK: - Turns

/// One finalized turn.
///
/// Sali writes into the page — no background, no border, no shape — inset by exactly one gutter, with the
/// mark in that gutter. You get the container: a raised card, left-aligned inside a right-aligned column,
/// with one asymmetric corner carrying ownership instead of contrast.
private struct TurnView: View {
    let message: ChatMessage
    var showsMark: Bool = true
    var userGap: CGFloat = ChatLayout.userMinGap
    var userMaxWidth: CGFloat? = nil
    var downloadedURL: URL? = nil
    var isDownloading: Bool = false
    var onDownloadFile: (() -> Void)? = nil
    var onRetry: (() -> Void)? = nil
    var onOpenImage: ((UIImage) -> Void)? = nil
    var onRetryImage: (() -> Void)? = nil
    var onRetryFile: (() -> Void)? = nil

    private var isUser: Bool { message.role == .user }
    private var isAgent: Bool { message.role == .agent }

    var body: some View {
        if isUser { userTurn } else { saliTurn }
    }

    // MARK: Sali

    private var saliTurn: some View {
        HStack(alignment: .top, spacing: 0) {
            markGutter
            VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                if isAgent { agentHeader }

                // A reply can carry BOTH an attachment and prose (a research report is a file + its
                // summary; a send-file reply is a card + a sentence). Render them STACKED, never
                // either/or — the old else-branch hid the summary of every file-bearing answer. An
                // IMAGE shows its text as an inline caption, so it isn't repeated beneath.
                if let attachment = message.attachment, attachment.isImage, let data = attachment.imageData {
                    ImageAttachmentView(data: data, caption: message.text,
                                        status: attachment.sendStatus,
                                        onOpen: { onOpenImage?($0) }, onRetry: { onRetryImage?() })
                } else {
                    if let attachment = message.attachment {
                        FileCardView(attachment: attachment, downloadedURL: downloadedURL,
                                     isDownloading: isDownloading, onDownload: onDownloadFile,
                                     onRetry: onRetryFile)
                    }
                    // The document column. `bleed` lets code blocks and tables reclaim the gutter, so the
                    // widest content on screen is the content that needs the width.
                    if !message.text.isEmpty {
                        MarkdownText(markdown: message.text, bleed: ChatLayout.gutter)
                            .textSelection(.enabled)
                            // Copy the whole reply. Selection alone means dragging handles through a long
                            // answer to get all of it; a long-press that yields the exact source Sali wrote
                            // — markdown intact, ready to paste into an editor — is what this is actually for.
                            .contextMenu {
                                Button {
                                    UIPasteboard.general.string = message.text
                                    Haptics.light()
                                } label: {
                                    Label("Copy reply", systemImage: "doc.on.doc")
                                }
                            }
                    }
                }

                if !message.steps.isEmpty {
                    ActivityTimeline(steps: message.steps)
                }

                // A VISIBLE copy control. A long-press context menu is invisible — nobody discovers a
                // gesture on a wall of text, so the affordance has to be on screen. Quiet enough to
                // ignore while reading, present enough to find without being told.
                if !message.text.isEmpty {
                    CopyReplyButton(text: message.text)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var markGutter: some View {
        Group {
            if showsMark {
                SaliMark(size: ChatLayout.markSize)
            }
        }
        .frame(width: ChatLayout.markColumn, alignment: .topLeading)
        .padding(.trailing, Theme.Spacing.s)
        .padding(.top, 1)          // optical: sits the ring on the first line's cap height
    }

    /// Sali-initiated turns are differentiated by STRUCTURE — a label and a rule that opens the turn like a
    /// section — never by being the one tinted object on a monochrome screen.
    private var agentHeader: some View {
        HStack(spacing: Theme.Spacing.s) {
            Image(systemName: agentIcon)
                .font(Theme.Typography.metadata)
            Text(agentLabel)
                .font(Theme.Typography.metadata)
                .textCase(.uppercase)
                .tracking(0.6)
                .fixedSize()
            Rectangle().fill(Theme.Colors.separator).frame(height: Theme.Stroke.hairline)
        }
        .foregroundStyle(Theme.Colors.tertiaryText)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(agentLabel)
    }

    private var agentLabel: String {
        switch message.agentImportance {
        case "question": "Sali · needs your answer"
        case "warning", "failure": "Sali · heads up"
        case "completion", "milestone": "Sali · update"
        default: "Sali · proactive"
        }
    }

    private var agentIcon: String {
        switch message.agentImportance {
        case "question": "questionmark.bubble"
        case "warning", "failure": "exclamationmark.triangle"
        case "completion", "milestone": "checkmark.seal"
        default: "sparkle"
        }
    }

    // MARK: You

    private var userTurn: some View {
        HStack(alignment: .top, spacing: 0) {
            // The cap on the user column: a floor on this spacer, not a flexible frame on the card — so a
            // short message hugs its own text and a long one wraps at 78%.
            Spacer(minLength: userGap)

            VStack(alignment: .trailing, spacing: Theme.Spacing.xs) {
                userContent
                if let status = message.sendStatus {
                    sendStatusFooter(status)
                }
            }
            // Trailing alignment, so the cap bounds a long message without stretching a short one: the
            // card still sizes to its own text and simply sits at the right edge of the capped column.
            .frame(maxWidth: userMaxWidth, alignment: .trailing)
        }
    }

    @ViewBuilder private var userContent: some View {
        if let attachment = message.attachment {
            if attachment.isImage, let data = attachment.imageData {
                ImageAttachmentView(data: data, caption: message.text, status: attachment.sendStatus,
                                    cornerRadius: ChatLayout.nestedRadius,
                                    onOpen: { onOpenImage?($0) }, onRetry: { onRetryImage?() })
                    .padding(Theme.Spacing.s)
                    .background(Theme.Colors.surfaceRaised, in: userShape)
                    .overlay(userShape.strokeBorder(Theme.Colors.border, lineWidth: Theme.Stroke.hairline))
            } else {
                FileCardView(attachment: attachment, downloadedURL: downloadedURL,
                             isDownloading: isDownloading, onDownload: onDownloadFile,
                             onRetry: onRetryFile, nested: true)
                    .padding(Theme.Spacing.s)
                    .background(Theme.Colors.surfaceRaised, in: userShape)
                    .overlay(userShape.strokeBorder(Theme.Colors.border, lineWidth: Theme.Stroke.hairline))
            }
        } else {
            // Left-aligned text inside a right-aligned container: the container states who is speaking,
            // so the text does not also have to be ragged-left to say it.
            Text(message.text)
                .font(Theme.Typography.body)
                .foregroundStyle(Theme.Colors.primaryText)
                .multilineTextAlignment(.leading)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, Theme.Spacing.m)
                .padding(.vertical, Theme.Spacing.s + 2)
                .background(Theme.Colors.surfaceRaised, in: userShape)
                .overlay(userShape.strokeBorder(Theme.Colors.border, lineWidth: Theme.Stroke.hairline))
                // Your own words are worth copying too — re-sending a long prompt with one word changed
                // is the common case, and retyping it is the thing that makes an app feel cheap.
                .contextMenu {
                    Button {
                        UIPasteboard.general.string = message.text
                        Haptics.light()
                    } label: {
                        Label("Copy", systemImage: "doc.on.doc")
                    }
                }
        }
    }

    /// Ownership as geometry: three soft corners and one tightened bottom-trailing corner, pointing at the
    /// person who wrote it. No ink slab, no inverted contrast.
    private var userShape: UnevenRoundedRectangle {
        UnevenRoundedRectangle(topLeadingRadius: Theme.Radius.l,
                               bottomLeadingRadius: Theme.Radius.l,
                               bottomTrailingRadius: Theme.Radius.xs,
                               topTrailingRadius: Theme.Radius.l,
                               style: .continuous)
    }

    /// Only states that are *live* get a footer. The old persistent checkmark appeared solely on turns sent
    /// from this device and vanished on the next reload, so it read as decaying state rather than delivery.
    @ViewBuilder private func sendStatusFooter(_ status: SendStatus) -> some View {
        switch status {
        case .sent:
            EmptyView()
        case .sending:
            Text("Sending…")
                .font(Theme.Typography.metadata)
                .foregroundStyle(Theme.Colors.tertiaryText)
                .padding(.trailing, Theme.Spacing.xs)
        case .queued:
            HStack(spacing: Theme.Spacing.xs) {
                Image(systemName: "hourglass")
                Text("Queued")
            }
            .font(Theme.Typography.metadata)
            .foregroundStyle(Theme.Colors.tertiaryText)
            .padding(.trailing, Theme.Spacing.xs)
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Queued behind Sali's current work")
        case .failed:
            Button { onRetry?() } label: {
                HStack(spacing: Theme.Spacing.xs) {
                    Image(systemName: "exclamationmark.circle")
                    Text("Not delivered — tap to retry")
                }
                .font(Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.danger)
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(.trailing, Theme.Spacing.xs)
            .accessibilityLabel("Message not delivered. Tap to retry.")
        }
    }
}

/// The copy affordance under one of Sali's replies. Shows what it did rather than relying on the
/// pasteboard being felt: the glyph and word swap to a confirmation for a moment, then return.
private struct CopyReplyButton: View {
    let text: String
    @State private var copied = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Button {
            UIPasteboard.general.string = text
            Haptics.light()
            withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick)) { copied = true }
            Task {
                try? await Task.sleep(nanoseconds: 1_600_000_000)
                withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick)) { copied = false }
            }
        } label: {
            HStack(spacing: Theme.Spacing.xs) {
                Image(systemName: copied ? "checkmark" : "doc.on.doc")
                    .font(.system(size: 11, weight: .semibold))
                Text(copied ? "Copied" : "Copy")
                    .font(Theme.Typography.metadata)
            }
            .foregroundStyle(copied ? Theme.Colors.primaryText : Theme.Colors.tertiaryText)
            .padding(.horizontal, Theme.Spacing.s)
            .frame(height: 44)                    // full-height target; the ink stays small
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.leading, -Theme.Spacing.s)      // optical: the glyph aligns to the text column edge
        .accessibilityLabel(copied ? "Copied" : "Copy reply")
    }
}

// MARK: - The live turn

/// The phase of the running turn, named in WORDS. Motion cannot say "which tool", and four looping
/// animations said nothing at all — so the line reports the phase and the tool name verbatim, because the
/// tool name IS the information.
private enum TurnPhase: Equatable {
    case queued
    case thinking
    case working(String)
    case writing

    var label: String {
        switch self {
        case .queued: "Queued — Sali is busy"
        case .thinking: "Thinking"
        case .working(let summary): summary
        case .writing: "Writing"
        }
    }

    var icon: String? {
        switch self {
        case .queued: "hourglass"
        default: nil
        }
    }
}

/// The live turn: ONE status line pinned at the top (so no phase change can ever reflow the prose beneath
/// it), the prose as it arrives, and the activity trail. Replaces the old thinking bubble, the in-bubble
/// activity chip, and the four ambient loops they ran between them.
private struct LiveTurnView: View {
    let text: String
    let phase: TurnPhase
    let startedAt: Date?
    let steps: [ActivityStep]

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            // The one ambient loop on screen. Vertically centred on the status row so the mark and the
            // phase word read as a single line.
            SaliMark(size: ChatLayout.markSize, animated: true)
                .frame(width: ChatLayout.markColumn, height: ChatLayout.statusRowHeight, alignment: .leading)
                .padding(.trailing, Theme.Spacing.s)

            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                // Activity FIRST (collapsed by default) - the record of the work sits above what
                // Sali is currently doing/saying, so the reader can pull it open when they want the
                // detail without ever having it forced on them. When there are no steps yet, the row
                // simply isn't there; nothing to expand until there is something to show.
                if !steps.isEmpty {
                    ActivityTimeline(steps: steps)
                }

                // Then the live status - "Writing", "Web search", the running tool name. This is the
                // one moving line on screen and it earns its position under the activity: what
                // happened, above what is happening now.
                TurnStatusLine(phase: phase, startedAt: startedAt)

                // Then the message itself. The stream lands here and only here; earlier iterations'
                // narration was moved into the activity trail above by the iteration_boundary event.
                if !text.isEmpty {
                    MarkdownText(markdown: text, bleed: ChatLayout.gutter)
                        .textSelection(.enabled)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle), value: phase)
    }
}

/// `phase · elapsed`. One row, one height, for the whole turn.
///
/// It REPORTS; it does not act. Stop lives in the composer, in one fixed place that is always on screen —
/// a second Stop here would only be reachable while the top of the turn happens to be scrolled into view,
/// and two controls for one action make the reader work out which one is authoritative.
private struct TurnStatusLine: View {
    let phase: TurnPhase
    let startedAt: Date?

    var body: some View {
        HStack(spacing: Theme.Spacing.s) {
            if let icon = phase.icon {
                Image(systemName: icon)
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .accessibilityHidden(true)
            }

            Text(phase.label)
                .font(Theme.Typography.footnote.weight(.medium))
                .foregroundStyle(Theme.Colors.secondaryText)
                .lineLimit(1)
                .truncationMode(.tail)

            if let startedAt {
                Text(verbatim: "·")
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .accessibilityHidden(true)
                // The clock is the honest part: it never claims progress, it only says how long this has
                // been running. Anchored to the turn's own start so the ticks don't drift.
                TimelineView(.periodic(from: startedAt, by: 1)) { context in
                    Text(elapsedLabel(from: startedAt, to: context.date))
                        .font(Theme.Typography.metadata.monospacedDigit())
                        .foregroundStyle(Theme.Colors.tertiaryText)
                }
            }

            Spacer(minLength: Theme.Spacing.s)
        }
        // The height is fixed even though the row now holds only text: the phase word changes several
        // times a turn, and a row that resized would push the prose beneath it every time.
        .frame(minHeight: ChatLayout.statusRowHeight)
        // One status element, not a control. `updatesFrequently` is what tells VoiceOver this value is
        // live, so it re-reads the phase and the clock instead of announcing them once and going quiet.
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.updatesFrequently)
    }

    private func elapsedLabel(from start: Date, to now: Date) -> String {
        let total = max(0, Int(now.timeIntervalSince(start)))
        return String(format: "%d:%02d", total / 60, total % 60)
    }
}

/// The record of what Sali actually did this turn, as a TIMELINE — a spine, a marker per step, and the
/// step's own words. Previously a grey list inside a grey box inside a bubble, which made the work look
/// like an error log.
private struct ActivityTimeline: View {
    let steps: [ActivityStep]
    @State private var expanded = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            Button {
                withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick)) { expanded.toggle() }
            } label: {
                HStack(spacing: Theme.Spacing.xs) {
                    Image(systemName: "chevron.right")
                        .font(Theme.Typography.metadata)
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                    Text(expanded ? "Hide steps" : "What Sali did")
                        .font(Theme.Typography.footnote)
                    Text("\(steps.count)")
                        .font(Theme.Typography.numeralSmall)
                        .foregroundStyle(Theme.Colors.tertiaryText)
                }
                .foregroundStyle(Theme.Colors.secondaryText)
                .frame(minHeight: 44, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(expanded ? "Hide Sali's steps" : "Show Sali's \(steps.count) steps")

            if expanded {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(steps.enumerated()), id: \.element.id) { index, step in
                        row(step, isLast: index == steps.count - 1)
                    }
                }
                .padding(.leading, Theme.Spacing.xs)
                .transition(.opacity)
            }
        }
    }

    private func row(_ step: ActivityStep, isLast: Bool) -> some View {
        HStack(alignment: .top, spacing: Theme.Spacing.m) {
            ZStack(alignment: .top) {
                if !isLast {
                    Rectangle()
                        .fill(Theme.Colors.separator)
                        .frame(width: Theme.Stroke.hairline)
                        .frame(maxHeight: .infinity)
                }
                marker(done: step.done)
            }
            .frame(width: 9)

            Text(step.label)
                .font(Theme.Typography.footnote)
                .foregroundStyle(step.done ? Theme.Colors.secondaryText : Theme.Colors.primaryText)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, isLast ? 0 : Theme.Spacing.m)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(step.done ? "\(step.label), done" : "\(step.label), running")
    }

    /// A filled marker for a finished step, a hollow one for a step still running. The spine passes behind
    /// it, so it carries a canvas-coloured halo to break the line cleanly.
    private func marker(done: Bool) -> some View {
        Group {
            if done {
                Circle().fill(Theme.Colors.secondaryText).frame(width: 5, height: 5)
            } else {
                Circle().strokeBorder(Theme.Colors.secondaryText, lineWidth: Theme.Stroke.hairline)
                    .frame(width: 6, height: 6)
            }
        }
        .frame(width: 9, height: 9)
        .background(Theme.Colors.background, in: Circle())
        .padding(.top, 5)          // optical: centres the marker on the step's first line
    }
}

// MARK: - Attachments

/// A file in the transcript — one Sali produced (incoming: download → ShareLink) or one you sent to the
/// task workspace (outgoing: shows delivery status). Monochrome, task-scoped (§10).
///
/// `nested` strips the card's own surface and edge: inside a user turn the container is already there, and
/// a second one would be exactly the box-in-a-box the redesign removed.
private struct FileCardView: View {
    let attachment: FileAttachment
    var downloadedURL: URL? = nil
    var isDownloading: Bool = false
    var onDownload: (() -> Void)? = nil
    var onRetry: (() -> Void)? = nil
    var nested: Bool = false

    var body: some View {
        HStack(spacing: Theme.Spacing.m) {
            Image(systemName: iconName)
                .font(Theme.Typography.titleSmall)
                .foregroundStyle(Theme.Colors.secondaryText)
                .frame(width: 26)
            VStack(alignment: .leading, spacing: 2) {
                Text(attachment.filename)
                    .font(Theme.Typography.body.weight(.medium))
                    .foregroundStyle(Theme.Colors.primaryText)
                    .lineLimit(1).truncationMode(.middle)
                Text(subtitle)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }
            Spacer(minLength: Theme.Spacing.s)
            trailing
        }
        .padding(nested ? 0 : Theme.Spacing.m)
        .frame(maxWidth: 320, alignment: .leading)
        .background(nested ? Color.clear : Theme.Colors.surfaceRaised)
        .clipShape(RoundedRectangle(cornerRadius: nested ? ChatLayout.nestedRadius : Theme.Radius.m,
                                    style: .continuous))
        .saliHairline(radius: nested ? ChatLayout.nestedRadius : Theme.Radius.m,
                      color: nested ? .clear : Theme.Colors.border)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private var trailing: some View {
        switch attachment.direction {
        case .incoming:
            if let url = downloadedURL {
                ShareLink(item: url) {
                    Image(systemName: "square.and.arrow.up")
                        .font(Theme.Typography.body)
                        .foregroundStyle(Theme.Colors.accent)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
            } else if isDownloading {
                ProgressView().frame(width: 44, height: 44)
            } else {
                Button { onDownload?() } label: {
                    Image(systemName: "arrow.down.circle")
                        .font(Theme.Typography.titleSmall)
                        .foregroundStyle(Theme.Colors.accent)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        case .outgoing:
            switch attachment.sendStatus {
            case .sending: ProgressView()
            case .failed:
                // A failed upload must be recoverable in place — the user shouldn't have to re-pick the file.
                if let onRetry {
                    Button(action: onRetry) {
                        HStack(spacing: Theme.Spacing.xs) {
                            Image(systemName: "arrow.clockwise")
                            Text("Retry")
                        }
                        .font(Theme.Typography.footnote.weight(.semibold))
                        .foregroundStyle(Theme.Colors.danger)
                        .frame(minHeight: 44)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Upload failed. Tap to retry.")
                } else {
                    Image(systemName: "exclamationmark.circle").foregroundStyle(Theme.Colors.danger)
                }
            default:
                EmptyView()
            }
        }
    }

    private var subtitle: String {
        var parts: [String] = []
        if let s = attachment.size {
            parts.append(ByteCountFormatter.string(fromByteCount: Int64(s), countStyle: .file))
        }
        switch attachment.direction {
        case .incoming:
            parts.append(downloadedURL != nil ? "Ready — share"
                         : (isDownloading ? "Downloading…" : "From Sali · tap to download"))
        case .outgoing:
            parts.append(attachment.sendStatus == .failed ? "Not delivered — tap Retry"
                         : (attachment.sendStatus == .sending ? "Uploading…" : "Sent to Sali's workspace"))
        }
        return parts.joined(separator: " · ")
    }

    private var iconName: String { saliFileIcon(for: attachment.filename) }
}

/// An inline image the user sent to Sali, with any caption below it (image + caption are one message).
/// Aspect-preserving thumbnail with a delivery-status badge; tap to view full-screen (or, if it failed,
/// tap to re-send). Sali *sees* the image — the backend folds a vision description into its reply.
/// The JPEG is decoded ONCE into @State (not in the parent's body, which re-runs while a reply streams).
private struct ImageAttachmentView: View {
    let data: Data
    var caption: String = ""
    var status: SendStatus?
    var cornerRadius: CGFloat = Theme.Radius.l
    var onOpen: (UIImage) -> Void = { _ in }
    var onRetry: () -> Void = {}

    @State private var uiImage: UIImage?

    private var isSending: Bool { status == .sending || status == .queued }
    private var isFailed: Bool { status == .failed }
    private var shape: RoundedRectangle {
        RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            thumbnail
            if !caption.isEmpty {
                Text(caption)
                    .font(Theme.Typography.body)
                    .foregroundStyle(Theme.Colors.primaryText)
                    .multilineTextAlignment(.leading)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: 244, alignment: .leading)
                    .padding(.horizontal, Theme.Spacing.xs)
            }
        }
        .task { if uiImage == nil, let img = UIImage(data: data) { uiImage = img.preparingForDisplay() ?? img } }
    }

    @ViewBuilder private var thumbnail: some View {
        Group {
            if let uiImage {
                Image(uiImage: uiImage).resizable().scaledToFit()
            } else {
                shape
                    .fill(Theme.Colors.placeholder)
                    .frame(width: 200, height: 200)
                    .overlay(ProgressView().controlSize(.small))
            }
        }
        .frame(maxWidth: 244, maxHeight: 288)
        .clipShape(shape)
        .overlay(shape.strokeBorder(isFailed ? Theme.Colors.danger : Theme.Colors.border,
                                    lineWidth: isFailed ? Theme.Stroke.emphasis : Theme.Stroke.hairline))
        .overlay { if isSending { scrim(0.28) { ProgressView().controlSize(.large).tint(.white) } } }
        .overlay { if isFailed { scrim(0.22) { EmptyView() } } }   // at-a-glance "not delivered"
        .overlay(alignment: .bottomTrailing) { badge.padding(Theme.Spacing.xs) }
        .contentShape(shape)
        .onTapGesture {
            if isFailed { onRetry() } else if !isSending, let uiImage { onOpen(uiImage) }
        }
        .accessibilityElement()
        .accessibilityLabel(a11yLabel)
        .accessibilityHint(isFailed || isSending ? "" : "Opens full screen")
        .accessibilityAddTraits(isFailed || (!isSending && uiImage != nil) ? .isButton : [])
    }

    private var a11yLabel: String {
        if isFailed { return "Photo failed to send. Tap to retry." }
        if isSending { return "Photo, uploading" }
        return caption.isEmpty ? "Photo sent to Sali" : "Photo sent to Sali. Caption: \(caption)"
    }

    private func scrim<Content: View>(_ opacity: Double, @ViewBuilder _ content: () -> Content) -> some View {
        ZStack { Color.black.opacity(opacity); content() }
            .clipShape(shape)
    }

    @ViewBuilder private var badge: some View {
        switch status {
        case .sending, .queued: EmptyView()   // the scrim already shows progress
        case .failed:
            Label("Retry", systemImage: "arrow.clockwise")
                .labelStyle(.titleAndIcon)
                .font(Theme.Typography.caption.weight(.semibold))
                .foregroundStyle(.white)
                .padding(.horizontal, Theme.Spacing.s).padding(.vertical, Theme.Spacing.xs + 2)
                .background(Theme.Colors.danger, in: Capsule())   // solid — legible over any photo
        default:
            EmptyView()
        }
    }
}

/// Identifiable wrapper so a picked image can drive a `fullScreenCover(item:)`.
private struct PreviewImage: Identifiable {
    let id = UUID()
    let image: UIImage
}

/// Full-screen photo viewer: pinch-zoom, double-tap-to-zoom, bounded pan when zoomed, drag-to-dismiss at
/// 1×, plus share + close. Monochrome chrome over a black stage. Honors Reduce Motion.
private struct FullScreenImageViewer: View {
    let image: UIImage
    let onClose: () -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    @State private var scale: CGFloat = 1
    @GestureState private var pinch: CGFloat = 1
    @State private var offset: CGSize = .zero       // committed pan
    @State private var dragOffset: CGSize = .zero   // live drag (pan or dismiss), animated back on release
    @State private var viewSize: CGSize = .zero

    private var effectiveScale: CGFloat { max(1, scale * pinch) }
    private var motion: Animation? {
        reduceMotion ? nil : .interactiveSpring(response: 0.3, dampingFraction: 0.85)
    }
    // At 1×, a drag dims the stage toward dismissal.
    private var dimming: CGFloat {
        guard effectiveScale <= 1.01 else { return 0 }
        return min(0.7, abs(dragOffset.height) / 500)
    }

    var body: some View {
        GeometryReader { geo in
            ZStack {
                Color.black.opacity(1 - dimming).ignoresSafeArea()

                Image(uiImage: image)
                    .resizable()
                    .scaledToFit()
                    .scaleEffect(effectiveScale)
                    .offset(x: offset.width + dragOffset.width, y: offset.height + dragOffset.height)
                    .gesture(magnification)
                    .simultaneousGesture(panOrDismiss)
                    .onTapGesture(count: 2) { toggleZoom() }
                    .ignoresSafeArea()

                controls
            }
            .onAppear { viewSize = geo.size }
            .onChange(of: geo.size) { _, s in viewSize = s }
        }
        .statusBarHidden(true)
        .animation(motion, value: scale)
        .animation(motion, value: offset)
        .animation(motion, value: dragOffset)
    }

    private var controls: some View {
        VStack {
            HStack {
                ShareLink(item: Image(uiImage: image),
                          preview: SharePreview("Photo", image: Image(uiImage: image))) {
                    chromeIcon("square.and.arrow.up")
                }
                .accessibilityLabel("Share")
                Spacer()
                Button(action: onClose) { chromeIcon("xmark") }
                    .accessibilityLabel("Close")
            }
            .padding(.horizontal, Theme.Spacing.m)
            .padding(.top, Theme.Spacing.xs)
            Spacer()
        }
    }

    private func chromeIcon(_ name: String) -> some View {
        Image(systemName: name)
            .font(Theme.Typography.body.weight(.semibold))
            .foregroundStyle(.white)
            .frame(width: 34, height: 34)
            .background(.ultraThinMaterial, in: Circle())
            .frame(width: 44, height: 44)          // ≥44pt hit target (visual circle stays 34)
            .contentShape(Rectangle())
            .environment(\.colorScheme, .dark)
    }

    private var magnification: some Gesture {
        MagnificationGesture()
            .updating($pinch) { value, state, _ in state = value }
            .onEnded { value in
                scale = min(4, max(1, scale * value))
                if scale <= 1.01 { scale = 1; offset = .zero } else { offset = clamp(offset, scale: scale) }
            }
    }

    private var panOrDismiss: some Gesture {
        DragGesture()
            .onChanged { value in dragOffset = value.translation }
            .onEnded { value in
                if effectiveScale <= 1.01 {
                    if abs(value.translation.height) > 140 { onClose() }
                    dragOffset = .zero   // animated snap-back below threshold (via .animation(value: dragOffset))
                } else {
                    let committed = CGSize(width: offset.width + value.translation.width,
                                           height: offset.height + value.translation.height)
                    offset = clamp(committed, scale: effectiveScale)
                    dragOffset = .zero
                }
            }
    }

    private func toggleZoom() {
        if scale > 1 { scale = 1; offset = .zero } else { scale = 2.5 }
    }

    /// Keep the scaled image's edges from crossing the screen edges (no dragging it off into the void).
    private func clamp(_ proposed: CGSize, scale: CGFloat) -> CGSize {
        guard viewSize != .zero, image.size.width > 0, image.size.height > 0 else { return proposed }
        let ar = image.size.width / image.size.height
        let viewAR = viewSize.width / viewSize.height
        let fitted = ar > viewAR
            ? CGSize(width: viewSize.width, height: viewSize.width / ar)
            : CGSize(width: viewSize.height * ar, height: viewSize.height)
        let maxX = max(0, (fitted.width * scale - viewSize.width) / 2)
        let maxY = max(0, (fitted.height * scale - viewSize.height) / 2)
        return CGSize(width: min(maxX, max(-maxX, proposed.width)),
                      height: min(maxY, max(-maxY, proposed.height)))
    }
}

/// Minimal file picker → returns a readable local copy URL (asCopy).
private struct DocumentPicker: UIViewControllerRepresentable {
    let onPick: (URL) -> Void
    func makeUIViewController(context: Context) -> UIDocumentPickerViewController {
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.item], asCopy: true)
        picker.delegate = context.coordinator
        picker.allowsMultipleSelection = false
        return picker
    }
    func updateUIViewController(_ vc: UIDocumentPickerViewController, context: Context) {}
    func makeCoordinator() -> Coordinator { Coordinator(onPick: onPick) }
    final class Coordinator: NSObject, UIDocumentPickerDelegate {
        let onPick: (URL) -> Void
        init(onPick: @escaping (URL) -> Void) { self.onPick = onPick }
        func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
            if let url = urls.first { onPick(url) }
        }
    }
}
