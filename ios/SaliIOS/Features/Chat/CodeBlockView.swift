import SwiftUI
import UIKit

/// A fenced code block (§7 "code blocks — language-aware, copy button, horizontal scroll"). Language
/// awareness is presentational only (a label) — there is no syntax highlighter, by design (no third-party
/// dependencies).
///
/// Two things changed with the transcript's redesign. The body fill is `surfaceSunken`, not `surface`:
/// under the elevation ramp ("higher = lighter, in both schemes") a `surface` fill sitting on the canvas is
/// *lighter* than what it sits on, so the code well was invisible — code is a recess in the page, like an
/// input well. And it now carries exactly ONE edge, because the assistant bubble that used to wrap it is
/// gone; everything nested in Sali's turn loses a border level.
struct CodeBlockView: View {
    let code: String
    let language: String?

    @State private var didCopy = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Rectangle()
                .fill(Theme.Colors.separator)
                .frame(height: Theme.Stroke.hairline)
            ScrollView(.horizontal, showsIndicators: false) {
                Text(code)
                    .font(Theme.Typography.mono)
                    .foregroundStyle(Theme.Colors.primaryText)
                    .lineSpacing(2)
                    .textSelection(.enabled)
                    // Code is the one content type that must NEVER be re-flowed: a wrapped line changes
                    // what the code means. `fixedSize` pins the text to its ideal (unwrapped) width so a
                    // long line makes the block scroll instead of folding or clipping.
                    .fixedSize(horizontal: true, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(Theme.Spacing.m)
            }
            .background(Theme.Colors.surfaceSunken)
        }
        .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.m, style: .continuous))
        .saliHairline(radius: Theme.Radius.m, color: Theme.Colors.border)
    }

    private var header: some View {
        HStack(spacing: Theme.Spacing.s) {
            Text(languageLabel)
                .font(Theme.Typography.metadata)
                .foregroundStyle(Theme.Colors.tertiaryText)
                .textCase(.uppercase)
                .tracking(0.6)

            Spacer(minLength: Theme.Spacing.s)

            Button(action: copy) {
                HStack(spacing: Theme.Spacing.xs) {
                    Image(systemName: didCopy ? "checkmark" : "doc.on.doc")
                    Text(didCopy ? "Copied" : "Copy")
                }
                .font(Theme.Typography.footnote.weight(.medium))
                .foregroundStyle(Theme.Colors.primaryText)
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(didCopy ? "Copied to clipboard" : "Copy code")
        }
        .padding(.leading, Theme.Spacing.m)
        .padding(.trailing, Theme.Spacing.m)
        .frame(height: 44)
        .background(Theme.Colors.surface)
    }

    private var languageLabel: String {
        guard let language, !language.isEmpty else { return "Code" }
        return language
    }

    private func copy() {
        UIPasteboard.general.string = code
        withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick)) { didCopy = true }
        Task {
            try? await Task.sleep(nanoseconds: UInt64(Theme.Motion.Duration.beat * 1_000_000_000))
            withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick)) { didCopy = false }
        }
    }
}
