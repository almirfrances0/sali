import SwiftUI

/// The primary screen (Prompt 13, §5/§7) — an iMessage-grade transcript over Sali's single shared
/// conversation. History loads once over REST; the live turn streams in purely from the WebSocket, folded by
/// `ChatViewModel`. Never shows chain-of-thought — only the bounded, high-level `activity` label.
struct ChatView: View {
    @EnvironmentObject private var appState: AppState
    @StateObject private var viewModel = ChatViewModel()

    @State private var draft = ""
    @State private var isAtBottom = true
    @FocusState private var inputFocused: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private let bottomAnchorID = "chat.bottom"

    var body: some View {
        NavigationStack {
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
            .navigationTitle("Sali")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    ConnectionBadge(appState.ws.state)
                }
            }
        }
        .task { await viewModel.load(api: appState.api) }
        .onChange(of: appState.latestEvent?.id) { _, _ in
            viewModel.ingestNew(from: appState.liveEvents)
        }
        .animation(reduceMotion ? nil : Theme.Motion.standard, value: viewModel.activity)
        .alert("Couldn't send", isPresented: Binding(
            get: { viewModel.sendError != nil },
            set: { if !$0 { viewModel.sendError = nil } }
        )) {
            Button("OK", role: .cancel) { viewModel.sendError = nil }
        } message: {
            Text(viewModel.sendError ?? "")
        }
    }

    // MARK: - Body states

    private var chatBody: some View {
        VStack(spacing: 0) {
            if appState.ws.state != .connected {
                offlineBanner
            }
            if viewModel.messages.isEmpty && viewModel.streaming == nil {
                EmptyStateView(
                    icon: "bubble.left.and.bubble.right",
                    title: "Start the conversation",
                    message: "Ask Sali anything — it remembers context across your whole day."
                )
            } else {
                transcript
            }
            activityChip
            inputBar
        }
    }

    private var offlineBanner: some View {
        HStack(spacing: Theme.Spacing.s) {
            Image(systemName: "wifi.slash")
            Text(appState.ws.state.label)
            Spacer()
        }
        .font(Theme.Typography.caption)
        .foregroundStyle(Theme.Colors.warn)
        .padding(.horizontal, Theme.Spacing.l)
        .padding(.vertical, Theme.Spacing.xs)
        .background(Theme.Colors.warn.opacity(0.1))
        .accessibilityElement(children: .combine)
    }

    // MARK: - Transcript

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: Theme.Spacing.m) {
                    ForEach(viewModel.messages) { message in
                        MessageBubble(message: message)
                            .id(message.id)
                    }
                    if let streaming = viewModel.streaming {
                        MessageBubble(message: streaming, isStreaming: true)
                            .id(streaming.id)
                    }
                    // Bottom sentinel: its visibility is our "at bottom?" signal — an iOS 17-compatible
                    // stand-in for iOS 18's onScrollGeometryChange. The LazyVStack only renders it when the
                    // newest content is on screen, so onAppear/onDisappear track whether we're pinned to the
                    // bottom (which gates auto-scroll and the "jump to latest" button).
                    Color.clear.frame(height: 1).id(bottomAnchorID)
                        .onAppear { isAtBottom = true }
                        .onDisappear { isAtBottom = false }
                }
                .padding(.horizontal, Theme.Spacing.l)
                .padding(.vertical, Theme.Spacing.m)
            }
            .onChange(of: viewModel.messages.count) { _, _ in scrollToBottom(proxy, force: isAtBottom) }
            .onChange(of: viewModel.streaming?.text) { _, _ in
                if isAtBottom { scrollToBottom(proxy, animated: false, force: true) }
            }
            .overlay(alignment: .bottomTrailing) {
                if !isAtBottom {
                    jumpToLatestButton(proxy)
                }
            }
        }
    }

    private func scrollToBottom(_ proxy: ScrollViewProxy, animated: Bool = true, force: Bool = false) {
        guard force else { return }
        if animated && !reduceMotion {
            withAnimation(Theme.Motion.quick) { proxy.scrollTo(bottomAnchorID, anchor: .bottom) }
        } else {
            proxy.scrollTo(bottomAnchorID, anchor: .bottom)
        }
    }

    private func jumpToLatestButton(_ proxy: ScrollViewProxy) -> some View {
        Button {
            isAtBottom = true
            scrollToBottom(proxy, force: true)
        } label: {
            Image(systemName: "arrow.down")
                .font(.system(size: 14, weight: .semibold))
                .padding(10)
                .background(.thinMaterial, in: Circle())
                .overlay(Circle().strokeBorder(Theme.Colors.separator.opacity(0.4)))
        }
        .padding(Theme.Spacing.m)
        .accessibilityLabel("Jump to latest message")
        .transition(reduceMotion ? .opacity : .scale.combined(with: .opacity))
    }

    // MARK: - Activity chip

    @ViewBuilder
    private var activityChip: some View {
        if let activity = viewModel.activity {
            HStack(spacing: Theme.Spacing.s) {
                ProgressView().controlSize(.mini)
                Text(activity).font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
            }
            .padding(.horizontal, Theme.Spacing.m)
            .padding(.vertical, Theme.Spacing.xs)
            .background(Theme.Colors.accentSoft, in: Capsule())
            .padding(.horizontal, Theme.Spacing.l)
            .padding(.bottom, Theme.Spacing.xs)
            .transition(.move(edge: .bottom).combined(with: .opacity))
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Sali: \(activity)")
        }
    }

    // MARK: - Input bar

    private var inputBar: some View {
        VStack(spacing: 0) {
            Divider()
            HStack(alignment: .bottom, spacing: Theme.Spacing.s) {
                TextField("Message Sali", text: $draft, axis: .vertical)
                    .lineLimit(1...6)
                    .font(Theme.Typography.body)
                    .padding(.horizontal, Theme.Spacing.m)
                    .padding(.vertical, Theme.Spacing.s)
                    .background(Theme.Colors.surface)
                    .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.l, style: .continuous))
                    .focused($inputFocused)
                    .disabled(!appState.role.canControl)
                    .submitLabel(.send)
                    .onSubmit { Task { await send() } }
                    .accessibilityHint(appState.role.canControl ? "" : "This device can only observe.")

                actionButton
            }
            .padding(.horizontal, Theme.Spacing.l)
            .padding(.vertical, Theme.Spacing.s)
        }
        .background(Theme.Colors.background)
    }

    @ViewBuilder
    private var actionButton: some View {
        if viewModel.isSending {
            Button {
                Task { await stop() }
            } label: {
                Image(systemName: "stop.fill")
                    .font(.system(size: 16, weight: .semibold))
                    .padding(10)
                    .background(Theme.Colors.danger.opacity(0.15), in: Circle())
                    .foregroundStyle(Theme.Colors.danger)
            }
            .accessibilityLabel("Stop Sali's response")
        } else {
            Button {
                Task { await send() }
            } label: {
                Image(systemName: "arrow.up")
                    .font(.system(size: 16, weight: .semibold))
                    .padding(10)
                    .background(sendEnabled ? Theme.Colors.accent : Theme.Colors.idle.opacity(0.3), in: Circle())
                    .foregroundStyle(.white)
            }
            .disabled(!sendEnabled)
            .accessibilityLabel("Send message")
        }
    }

    private var sendEnabled: Bool {
        !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && appState.role.canControl
            && !viewModel.isSending
    }

    private func send() async {
        guard sendEnabled else { return }
        let text = draft
        draft = ""
        isAtBottom = true
        await viewModel.send(text, api: appState.api)
    }

    private func stop() async {
        try? await appState.api.postVoid("conversation/cancel")
    }
}

// MARK: - Message bubble

private struct MessageBubble: View {
    let message: ChatMessage
    var isStreaming: Bool = false

    var body: some View {
        HStack(alignment: .bottom, spacing: 0) {
            if message.role == .user { Spacer(minLength: 40) }

            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                if message.isAgentInitiated {
                    HStack(spacing: 4) {
                        Image(systemName: "sparkles")
                        Text("Sali").font(Theme.Typography.caption.weight(.semibold))
                    }
                    .foregroundStyle(Theme.Colors.accent)
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("From Sali, unprompted")
                }

                if isStreaming && message.text.isEmpty {
                    TypingIndicator()
                } else {
                    content
                    if isStreaming {
                        TypingIndicator()
                    }
                }
            }
            .padding(.horizontal, Theme.Spacing.m)
            .padding(.vertical, Theme.Spacing.s)
            .background(bubbleBackground)
            .overlay(bubbleBorder)
            .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.l, style: .continuous))
            .foregroundStyle(message.role == .user ? .white : Theme.Colors.primaryText)
            // Plain user text is safely combined into one VoiceOver utterance. Assistant/agent bubbles render
            // live Markdown that can contain an interactive Copy button (inside a code block) — those stay
            // un-combined so every control remains individually reachable rather than being flattened away.
            .modifier(CombinedLabelIfPlainText(message: message))

            if message.role != .user { Spacer(minLength: 40) }
        }
    }

    @ViewBuilder
    private var content: some View {
        if message.role == .user {
            Text(message.text)
                .font(Theme.Typography.body)
                .textSelection(.enabled)
        } else {
            MarkdownText(markdown: message.text)
        }
    }

    private var bubbleBackground: some View {
        (message.role == .user ? Theme.Colors.accent : Theme.Colors.surface)
    }

    @ViewBuilder
    private var bubbleBorder: some View {
        if message.isAgentInitiated {
            RoundedRectangle(cornerRadius: Theme.Radius.l, style: .continuous)
                .strokeBorder(Theme.Colors.accent.opacity(0.5), lineWidth: 1.5)
        }
    }
}

private struct CombinedLabelIfPlainText: ViewModifier {
    let message: ChatMessage
    func body(content: Content) -> some View {
        if message.role == .user {
            content
                .accessibilityElement(children: .combine)
                .accessibilityLabel("You: \(message.text)")
        } else {
            content
        }
    }
}

/// A calm three-dot "typing" cue driven by `TimelineView` (no timers, no stored animation state). Collapses
/// to a static row when Reduce Motion is on.
private struct TypingIndicator: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Group {
            if reduceMotion {
                dots(highlighting: -1)
            } else {
                TimelineView(.periodic(from: .now, by: 0.35)) { timeline in
                    let phase = Int(timeline.date.timeIntervalSinceReferenceDate / 0.35) % 3
                    dots(highlighting: phase)
                }
            }
        }
        .accessibilityHidden(true)
    }

    private func dots(highlighting phase: Int) -> some View {
        HStack(spacing: 4) {
            ForEach(0..<3, id: \.self) { i in
                Circle()
                    .fill(Theme.Colors.secondaryText)
                    .frame(width: 5, height: 5)
                    .opacity(i == phase ? 1 : 0.35)
            }
        }
    }
}
