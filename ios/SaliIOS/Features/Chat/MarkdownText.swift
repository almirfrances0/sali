import SwiftUI

// A dependency-free Markdown renderer (§7 "Markdown: assistant content is Markdown"). Fenced code blocks are
// split out by hand first — `AttributedString(markdown:)` doesn't render them well — everything else
// (headings, lists, blockquotes, rules, simple tables) is parsed line-by-line into blocks, and inline spans
// within each block (bold/italic/inline code/links) are handed to `AttributedString(markdown:)` so we never
// reinvent inline parsing.

/// One parsed Markdown block, tagged with a stable sequential id so `ForEach` identity survives re-parses of
/// the same source string (a streaming assistant bubble re-parses on every delta).
private struct MarkdownBlockItem: Identifiable {
    let id: Int
    let block: MarkdownBlock
}

private enum MarkdownBlock {
    case heading(level: Int, text: String)
    case paragraph(text: String)
    case unorderedList(items: [String])
    case orderedList(items: [String])
    case blockquote(text: String)
    case horizontalRule
    case codeBlock(code: String, language: String?)
    case table(headers: [String], rows: [[String]])
}

private enum MarkdownParser {
    static func parse(_ markdown: String) -> [MarkdownBlockItem] {
        let lines = markdown.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n")
        var blocks: [MarkdownBlock] = []
        var paragraphBuffer: [String] = []
        var i = 0

        func flushParagraph() {
            guard !paragraphBuffer.isEmpty else { return }
            let text = paragraphBuffer.joined(separator: " ").trimmingCharacters(in: .whitespaces)
            if !text.isEmpty { blocks.append(.paragraph(text: text)) }
            paragraphBuffer.removeAll()
        }

        while i < lines.count {
            let trimmed = lines[i].trimmingCharacters(in: .whitespaces)

            // Fenced code block — consumed verbatim, no inline parsing inside.
            if let language = fenceLanguage(trimmed) {
                flushParagraph()
                i += 1
                var codeLines: [String] = []
                while i < lines.count, !isFence(lines[i]) {
                    codeLines.append(lines[i])
                    i += 1
                }
                if i < lines.count { i += 1 } // consume the closing fence
                blocks.append(.codeBlock(code: codeLines.joined(separator: "\n"),
                                          language: language.isEmpty ? nil : language))
                continue
            }

            if trimmed.isEmpty {
                flushParagraph()
                i += 1
                continue
            }

            if isHorizontalRule(trimmed) {
                flushParagraph()
                blocks.append(.horizontalRule)
                i += 1
                continue
            }

            if let (level, text) = headingMatch(trimmed) {
                flushParagraph()
                blocks.append(.heading(level: level, text: text))
                i += 1
                continue
            }

            // Table: this line looks like a row and the next is a `---|---` separator.
            if i + 1 < lines.count, looksLikeTableRow(trimmed),
               isTableSeparator(lines[i + 1]) {
                flushParagraph()
                let headers = splitTableRow(trimmed)
                i += 2
                var rows: [[String]] = []
                while i < lines.count, looksLikeTableRow(lines[i].trimmingCharacters(in: .whitespaces)) {
                    rows.append(splitTableRow(lines[i]))
                    i += 1
                }
                blocks.append(.table(headers: headers, rows: rows))
                continue
            }

            if trimmed.hasPrefix(">") {
                flushParagraph()
                var quoteLines: [String] = []
                while i < lines.count, lines[i].trimmingCharacters(in: .whitespaces).hasPrefix(">") {
                    var q = lines[i].trimmingCharacters(in: .whitespaces)
                    q.removeFirst()
                    if q.hasPrefix(" ") { q.removeFirst() }
                    quoteLines.append(q)
                    i += 1
                }
                blocks.append(.blockquote(text: quoteLines.joined(separator: "\n")))
                continue
            }

            if let item = unorderedItem(trimmed) {
                flushParagraph()
                var items = [item]
                i += 1
                while i < lines.count, let next = unorderedItem(lines[i].trimmingCharacters(in: .whitespaces)) {
                    items.append(next)
                    i += 1
                }
                blocks.append(.unorderedList(items: items))
                continue
            }

            if let item = orderedItem(trimmed) {
                flushParagraph()
                var items = [item]
                i += 1
                while i < lines.count, let next = orderedItem(lines[i].trimmingCharacters(in: .whitespaces)) {
                    items.append(next)
                    i += 1
                }
                blocks.append(.orderedList(items: items))
                continue
            }

            paragraphBuffer.append(trimmed)
            i += 1
        }
        flushParagraph()

        return blocks.enumerated().map { MarkdownBlockItem(id: $0.offset, block: $0.element) }
    }

    // MARK: - Line matchers

    private static func fenceLanguage(_ line: String) -> String? {
        guard line.hasPrefix("```") else { return nil }
        return String(line.dropFirst(3)).trimmingCharacters(in: .whitespaces)
    }

    private static func isFence(_ line: String) -> Bool {
        line.trimmingCharacters(in: .whitespaces).hasPrefix("```")
    }

    private static func isHorizontalRule(_ line: String) -> Bool {
        let stripped = line.replacingOccurrences(of: " ", with: "")
        guard stripped.count >= 3, let marker = stripped.first, "-*_".contains(marker) else { return false }
        return stripped.allSatisfy { $0 == marker }
    }

    private static func headingMatch(_ line: String) -> (Int, String)? {
        var level = 0
        var idx = line.startIndex
        while idx < line.endIndex, line[idx] == "#", level < 6 {
            level += 1
            idx = line.index(after: idx)
        }
        guard level > 0, idx < line.endIndex, line[idx] == " " else { return nil }
        let text = line[line.index(after: idx)...].trimmingCharacters(in: .whitespaces)
        return (level, text)
    }

    private static func looksLikeTableRow(_ line: String) -> Bool {
        !line.isEmpty && line.contains("|")
    }

    private static func isTableSeparator(_ line: String) -> Bool {
        let t = line.trimmingCharacters(in: .whitespaces)
        guard t.contains("-") else { return false }
        let allowed = CharacterSet(charactersIn: "-:| ")
        return t.unicodeScalars.allSatisfy { allowed.contains($0) }
    }

    private static func splitTableRow(_ line: String) -> [String] {
        var t = line.trimmingCharacters(in: .whitespaces)
        if t.hasPrefix("|") { t.removeFirst() }
        if t.hasSuffix("|") { t.removeLast() }
        return t.components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }
    }

    private static func unorderedItem(_ line: String) -> String? {
        for marker in ["- ", "* ", "+ "] where line.hasPrefix(marker) {
            return String(line.dropFirst(marker.count))
        }
        return nil
    }

    private static func orderedItem(_ line: String) -> String? {
        guard let markerIdx = line.firstIndex(where: { $0 == "." || $0 == ")" }) else { return nil }
        let prefix = line[line.startIndex..<markerIdx]
        guard !prefix.isEmpty, prefix.allSatisfy(\.isNumber) else { return nil }
        let afterMarker = line.index(after: markerIdx)
        guard afterMarker < line.endIndex, line[afterMarker] == " " else { return nil }
        return String(line[line.index(after: afterMarker)...])
    }
}

/// Renders a Markdown string with no third-party dependency. Inline spans (bold/italic/inline code/links)
/// are delegated to `AttributedString(markdown:)`; block structure is parsed above.
struct MarkdownText: View {
    let markdown: String

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            ForEach(MarkdownParser.parse(markdown)) { item in
                blockView(for: item.block)
            }
        }
        .textSelection(.enabled)
    }

    @ViewBuilder
    private func blockView(for block: MarkdownBlock) -> some View {
        switch block {
        case let .heading(level, text):
            Text(inline(text))
                .font(headingFont(for: level))
                .accessibilityAddTraits(.isHeader)
                .padding(.top, level == 1 ? Theme.Spacing.xs : 0)

        case let .paragraph(text):
            Text(inline(text))
                .font(Theme.Typography.body)
                .fixedSize(horizontal: false, vertical: true)

        case let .unorderedList(items):
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .top, spacing: Theme.Spacing.s) {
                        Text("•").font(Theme.Typography.body)
                        Text(inline(item)).font(Theme.Typography.body)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }

        case let .orderedList(items):
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                    HStack(alignment: .top, spacing: Theme.Spacing.s) {
                        Text("\(index + 1).").font(Theme.Typography.body).monospacedDigit()
                        Text(inline(item)).font(Theme.Typography.body)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }

        case let .blockquote(text):
            HStack(spacing: Theme.Spacing.s) {
                RoundedRectangle(cornerRadius: 1.5).fill(Theme.Colors.accent.opacity(0.6)).frame(width: 3)
                Text(inline(text))
                    .font(Theme.Typography.body)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.vertical, Theme.Spacing.xs)

        case .horizontalRule:
            Rectangle().fill(Theme.Colors.separator).frame(height: 1)
                .padding(.vertical, Theme.Spacing.xs)

        case let .codeBlock(code, language):
            CodeBlockView(code: code, language: language)

        case let .table(headers, rows):
            tableView(headers: headers, rows: rows)
        }
    }

    private func tableView(headers: [String], rows: [[String]]) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            Grid(alignment: .topLeading, horizontalSpacing: Theme.Spacing.m, verticalSpacing: Theme.Spacing.s) {
                GridRow {
                    ForEach(Array(headers.enumerated()), id: \.offset) { _, header in
                        Text(inline(header)).font(Theme.Typography.body.weight(.semibold))
                    }
                }
                Divider().gridCellColumns(max(headers.count, 1))
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    GridRow {
                        ForEach(Array(row.enumerated()), id: \.offset) { _, cell in
                            Text(inline(cell)).font(Theme.Typography.body)
                        }
                    }
                }
            }
            .padding(Theme.Spacing.s)
        }
        .background(Theme.Colors.surface)
        .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.s, style: .continuous))
    }

    private func headingFont(for level: Int) -> Font {
        switch level {
        case 1: Theme.Typography.title
        case 2: Theme.Typography.heading
        default: Theme.Typography.body.weight(.semibold)
        }
    }

    /// Inline spans (bold/italic/inline code/links) via Foundation's Markdown parser; plain text on failure
    /// — never crash on malformed model output.
    private func inline(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
    }
}
