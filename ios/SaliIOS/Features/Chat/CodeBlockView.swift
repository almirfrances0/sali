import SwiftUI
import UIKit

/// A fenced code block (§7 "code blocks — language-aware, copy button, horizontal scroll"). Language
/// awareness is presentational only (a label) — there is no syntax highlighter, by design (no third-party
/// dependencies).
struct CodeBlockView: View {
    let code: String
    let language: String?

    @State private var didCopy = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            ScrollView(.horizontal, showsIndicators: false) {
                Text(code)
                    .font(Theme.Typography.mono)
                    .foregroundStyle(Theme.Colors.primaryText)
                    .padding(Theme.Spacing.m)
            }
            .background(Theme.Colors.surface)
        }
        .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.m, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.m, style: .continuous)
                .strokeBorder(Theme.Colors.separator.opacity(0.6), lineWidth: 1)
        )
    }

    private var header: some View {
        HStack {
            Text(languageLabel)
                .font(Theme.Typography.caption.weight(.semibold))
                .foregroundStyle(Theme.Colors.secondaryText)
                .textCase(.uppercase)

            Spacer()

            Button(action: copy) {
                HStack(spacing: Theme.Spacing.xs) {
                    Image(systemName: didCopy ? "checkmark" : "doc.on.doc")
                    Text(didCopy ? "Copied" : "Copy")
                }
                .font(Theme.Typography.caption.weight(.medium))
            }
            .buttonStyle(.plain)
            .foregroundStyle(didCopy ? Theme.Colors.ok : Theme.Colors.accent)
            .accessibilityLabel(didCopy ? "Copied to clipboard" : "Copy code")
        }
        .padding(.horizontal, Theme.Spacing.m)
        .padding(.vertical, Theme.Spacing.s)
        .background(Theme.Colors.surfaceRaised)
    }

    private var languageLabel: String {
        guard let language, !language.isEmpty else { return "Code" }
        return language
    }

    private func copy() {
        UIPasteboard.general.string = code
        withAnimation(reduceMotion ? nil : Theme.Motion.quick) { didCopy = true }
        Task {
            try? await Task.sleep(nanoseconds: 1_400_000_000)
            withAnimation(reduceMotion ? nil : Theme.Motion.quick) { didCopy = false }
        }
    }
}
