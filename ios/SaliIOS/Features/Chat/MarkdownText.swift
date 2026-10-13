import SwiftUI

// A dependency-free Markdown renderer (§7 "Markdown: assistant content is Markdown"). Block structure —
// headings, lists (nested), blockquotes, rules, tables, fenced code — is parsed here line by line; the
// inline spans inside each block (bold/italic/strikethrough/inline code/links) are handed to
// `AttributedString(markdown:)` so we never reinvent inline parsing.
//
// Two things the model actually emits that Foundation will not render on its own, and which this file
// therefore handles before anything else sees them:
//
// 1. **Raw HTML.** `AttributedString(markdown:)` prints `<code>x</code>` verbatim, angle brackets and all.
//    Simple inline tags are normalized into the Markdown that means the same thing, entities are decoded,
//    and any tag left over is dropped rather than printed — see `MarkdownInline`. `<pre>` blocks become
//    real code blocks.
// 2. **Nesting.** Lists were flattened: every line was trimmed before it was matched, so an indented
//    sub-item lost the one piece of information that made it a sub-item. Indentation is now measured.

// MARK: - Parsed model

private enum MarkdownListMarker {
    case bullet
    case number(Int)
    case task(Bool)
}

private struct MarkdownListItem: Identifiable {
    let id: Int
    /// 0-based nesting level, derived from measured indentation rather than a fixed step, so 2-space and
    /// 4-space lists (models emit both, sometimes in one reply) nest identically.
    let depth: Int
    let marker: MarkdownListMarker
    var text: String
}

private struct MarkdownTableModel {
    let headers: [String]
    /// Column alignment from the separator row's colons (`:---`, `---:`, `:---:`).
    let alignments: [HorizontalAlignment]
    /// Always exactly `headers.count` wide — ragged rows are padded/trimmed at parse time, because a
    /// short row silently shifts every cell after it into the wrong column.
    let rows: [[String]]
}

private enum MarkdownBlock {
    case heading(level: Int, text: String)
    case paragraph(text: String)
    case list(items: [MarkdownListItem])
    case blockquote(blocks: [MarkdownBlockItem])
    case horizontalRule
    case codeBlock(code: String, language: String?)
    case table(MarkdownTableModel)
}

/// One parsed block, tagged with a stable sequential id so `ForEach` identity survives re-parses of the
/// same source string (a streaming assistant bubble re-parses on every delta).
private struct MarkdownBlockItem: Identifiable {
    let id: Int
    let block: MarkdownBlock
}

// MARK: - Parser

private struct MarkdownParser {
    private let lines: [String]
    private var i = 0
    private var nextID = 0
    private let quoteDepth: Int

    static func parse(_ markdown: String) -> [MarkdownBlockItem] {
        var parser = MarkdownParser(source: markdown, quoteDepth: 0)
        return parser.run()
    }

    private init(source: String, quoteDepth: Int) {
        lines = source.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n")
        self.quoteDepth = quoteDepth
    }

    private mutating func run() -> [MarkdownBlockItem] {
        var items: [MarkdownBlockItem] = []
        var paragraph = ""
        var previousLineHardBroke = false

        func emit(_ block: MarkdownBlock) {
            items.append(MarkdownBlockItem(id: nextID, block: block))
            nextID += 1
        }

        func flushParagraph() {
            let text = paragraph.trimmingCharacters(in: .whitespacesAndNewlines)
            paragraph = ""
            previousLineHardBroke = false
            if !text.isEmpty { emit(.paragraph(text: text)) }
        }

        while i < lines.count {
            let raw = lines[i]
            let trimmed = raw.trimmingCharacters(in: .whitespaces)

            if let fence = Fence(opening: trimmed) {
                flushParagraph()
                emit(codeBlock(openedBy: fence))
                continue
            }
            if let html = htmlCodeBlock() {
                flushParagraph()
                emit(html)
                continue
            }
            if trimmed.isEmpty {
                flushParagraph()
                i += 1
                continue
            }
            if Self.isHorizontalRule(trimmed) {
                flushParagraph()
                emit(.horizontalRule)
                i += 1
                continue
            }
            if let (level, text) = Self.headingMatch(trimmed) {
                flushParagraph()
                emit(.heading(level: level, text: text))
                i += 1
                continue
            }
            if let table = tableAtCursor() {
                flushParagraph()
                emit(.table(table))
                continue
            }
            if trimmed.hasPrefix(">") {
                flushParagraph()
                emit(blockquote())
                continue
            }
            if Self.listMarker(in: raw) != nil {
                flushParagraph()
                emit(list())
                continue
            }

            // Ordinary prose. A trailing double-space or backslash is Markdown's hard break and is the
            // only way a model can put two lines in one paragraph — it used to be silently collapsed.
            var piece = trimmed
            let hardBreak = raw.hasSuffix("  ") || piece.hasSuffix("\\")
            if piece.hasSuffix("\\") { piece.removeLast() }
            if !paragraph.isEmpty { paragraph += previousLineHardBroke ? "\n" : " " }
            paragraph += piece
            previousLineHardBroke = hardBreak
            i += 1
        }
        flushParagraph()
        return items
    }

    // MARK: Fenced code

    /// An opening fence, remembering its marker and width so ```` ```` ```` closes only on a run at least
    /// as long — a fenced block containing a triple-backtick example no longer ends halfway through.
    private struct Fence {
        let marker: Character
        let width: Int
        let language: String?

        init?(opening line: String) {
            guard let first = line.first, first == "`" || first == "~" else { return nil }
            let run = line.prefix { $0 == first }.count
            guard run >= 3 else { return nil }
            let info = line.dropFirst(run).trimmingCharacters(in: .whitespaces)
            if first == "`", info.contains("`") { return nil }   // not a fence: an inline code span
            marker = first
            width = run
            let name = info.split(separator: " ").first.map(String.init)
            language = (name?.isEmpty ?? true) ? nil : name
        }
    }

    private mutating func codeBlock(openedBy fence: Fence) -> MarkdownBlock {
        i += 1
        var body: [String] = []
        while i < lines.count {
            let trimmed = lines[i].trimmingCharacters(in: .whitespaces)
            let run = trimmed.prefix { $0 == fence.marker }.count
            let isClosing = run >= fence.width
                && trimmed.dropFirst(run).trimmingCharacters(in: .whitespaces).isEmpty
            if isClosing {
                i += 1
                break
            }
            body.append(lines[i])
            i += 1
        }
        return .codeBlock(code: Self.dedented(body), language: fence.language)
    }

    /// `<pre>…</pre>` is a code block written in HTML. Rendering it as prose printed the tags and lost
    /// every line break; rendering it as code is what the author meant.
    private mutating func htmlCodeBlock() -> MarkdownBlock? {
        let opening = lines[i].trimmingCharacters(in: .whitespaces).lowercased()
        guard opening.hasPrefix("<pre") else { return nil }
        var body: [String] = []
        var cursor = i
        var closed = false
        while cursor < lines.count {
            body.append(lines[cursor])
            if lines[cursor].lowercased().contains("</pre>") {
                closed = true
                cursor += 1
                break
            }
            cursor += 1
        }
        // An unterminated `<pre>` is not a block we understand — fall through and let prose handle it.
        guard closed else { return nil }
        i = cursor
        let joined = body.joined(separator: "\n")
        let code = MarkdownInline.plainText(fromHTML: joined)
        return .codeBlock(code: code.trimmingCharacters(in: .whitespacesAndNewlines),
                          language: MarkdownInline.languageClass(in: joined))
    }

    /// Strip the deepest common indent so a fence written inside a list item doesn't render pre-indented.
    private static func dedented(_ body: [String]) -> String {
        let indents = body.filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
            .map { $0.prefix { $0 == " " }.count }
        guard let common = indents.min(), common > 0 else { return body.joined(separator: "\n") }
        return body.map { String($0.dropFirst(min(common, $0.prefix { $0 == " " }.count))) }
            .joined(separator: "\n")
    }

    // MARK: Blockquote

    private mutating func blockquote() -> MarkdownBlock {
        var inner: [String] = []
        while i < lines.count {
            let trimmed = lines[i].trimmingCharacters(in: .whitespaces)
            guard trimmed.hasPrefix(">") else { break }
            var quoted = String(trimmed.dropFirst())
            if quoted.hasPrefix(" ") { quoted.removeFirst() }
            inner.append(quoted)
            i += 1
        }
        // A quote can hold a list, a heading, even code — so its contents get the same parser, not a
        // single flattened `Text`. Bounded, because quoted quoted quoted quotes are not a design goal.
        guard quoteDepth < 3 else {
            return .paragraph(text: inner.joined(separator: " "))
        }
        var nested = MarkdownParser(source: inner.joined(separator: "\n"), quoteDepth: quoteDepth + 1)
        return .blockquote(blocks: nested.run())
    }

    // MARK: Lists

    private mutating func list() -> MarkdownBlock {
        var items: [MarkdownListItem] = []
        var indentStack: [Int] = []
        var nextItemID = 0

        while i < lines.count {
            let raw = lines[i]
            let trimmed = raw.trimmingCharacters(in: .whitespaces)

            if trimmed.isEmpty {
                // A loose list survives ONE blank line between items; two ends it. So does a blank line
                // followed by a marker of the other family — a bulleted list and the numbered list under
                // it are two blocks, and merging them collapsed the air between them.
                guard i + 1 < lines.count,
                      let next = Self.listMarker(in: lines[i + 1]),
                      Self.sameFamily(next.marker, as: items.last?.marker) else { break }
                i += 1
                continue
            }
            if Fence(opening: trimmed) != nil { break }

            guard let marker = Self.listMarker(in: raw) else {
                // An indented, non-marker line continues the item above it rather than starting a
                // paragraph that visually escapes the list.
                let indent = raw.prefix { $0 == " " || $0 == "\t" }.count
                guard !items.isEmpty, indent >= 2 else { break }
                items[items.count - 1].text += " " + trimmed
                i += 1
                continue
            }

            items.append(MarkdownListItem(id: nextItemID,
                                          depth: Self.depth(for: marker.indent, in: &indentStack),
                                          marker: marker.marker,
                                          text: marker.text))
            nextItemID += 1
            i += 1
        }
        return .list(items: items)
    }

    /// Bullets and task items are one family; numbers are another. Only used to decide whether a blank
    /// line ends the list or merely loosens it.
    private static func sameFamily(_ marker: MarkdownListMarker, as other: MarkdownListMarker?) -> Bool {
        guard let other else { return true }
        switch (marker, other) {
        case (.number, .number): return true
        case (.number, _), (_, .number): return false
        default: return true
        }
    }

    /// Depth from measured indentation: each new, deeper indent pushes a level and a shallower one pops
    /// back to it. This is what makes 2-space and 4-space nesting render the same.
    private static func depth(for indent: Int, in stack: inout [Int]) -> Int {
        while let last = stack.last, indent < last { stack.removeLast() }
        if let last = stack.last {
            if indent > last { stack.append(indent) }
        } else {
            stack.append(indent)
        }
        return min(4, max(0, stack.count - 1))
    }

    private static func listMarker(in raw: String) -> (indent: Int, marker: MarkdownListMarker, text: String)? {
        var indent = 0
        var idx = raw.startIndex
        while idx < raw.endIndex, raw[idx] == " " || raw[idx] == "\t" {
            indent += raw[idx] == "\t" ? 4 : 1
            idx = raw.index(after: idx)
        }
        guard indent <= 16 else { return nil }
        let rest = raw[idx...]
        guard let first = rest.first else { return nil }

        if "-*+".contains(first) {
            let after = rest.dropFirst()
            guard after.hasPrefix(" ") else { return nil }
            let text = String(after.dropFirst()).trimmingCharacters(in: .whitespaces)
            if let task = taskMarker(text) { return (indent, .task(task.done), task.text) }
            return (indent, .bullet, text)
        }

        let digits = rest.prefix { $0.isNumber }
        guard !digits.isEmpty, digits.count <= 9 else { return nil }
        let afterDigits = rest.dropFirst(digits.count)
        guard let separator = afterDigits.first, separator == "." || separator == ")" else { return nil }
        let body = afterDigits.dropFirst()
        guard body.hasPrefix(" ") else { return nil }
        let text = String(body.dropFirst()).trimmingCharacters(in: .whitespaces)
        if let task = taskMarker(text) { return (indent, .task(task.done), task.text) }
        return (indent, .number(Int(digits) ?? 1), text)
    }

    /// GitHub task syntax — `- [ ]` / `- [x]`. Models reach for it constantly for plans and checklists,
    /// and it used to render as literal brackets.
    private static func taskMarker(_ text: String) -> (done: Bool, text: String)? {
        let lower = text.lowercased()
        for (prefix, done) in [("[ ]", false), ("[x]", true)] where lower.hasPrefix(prefix) {
            let rest = String(text.dropFirst(prefix.count)).trimmingCharacters(in: .whitespaces)
            guard rest.isEmpty || text.dropFirst(prefix.count).hasPrefix(" ") else { return nil }
            return (done, rest)
        }
        return nil
    }

    // MARK: Tables

    private mutating func tableAtCursor() -> MarkdownTableModel? {
        guard i + 1 < lines.count else { return nil }
        let headerLine = lines[i].trimmingCharacters(in: .whitespaces)
        guard headerLine.contains("|") else { return nil }
        guard let alignments = Self.tableAlignments(lines[i + 1].trimmingCharacters(in: .whitespaces)) else {
            return nil
        }
        let headers = Self.splitRow(headerLine)
        // The separator has to agree with the header about the number of columns, or this is prose that
        // happens to contain a pipe followed by a line of dashes.
        guard headers.count == alignments.count, headers.count >= 1 else { return nil }
        guard headers.count > 1 || headerLine.hasPrefix("|") else { return nil }

        i += 2
        var rows: [[String]] = []
        while i < lines.count {
            let trimmed = lines[i].trimmingCharacters(in: .whitespaces)
            guard !trimmed.isEmpty, trimmed.contains("|") else { break }
            var cells = Self.splitRow(trimmed)
            if cells.count < headers.count {
                cells += Array(repeating: "", count: headers.count - cells.count)
            } else if cells.count > headers.count {
                cells = Array(cells.prefix(headers.count))
            }
            rows.append(cells)
            i += 1
        }
        return MarkdownTableModel(headers: headers, alignments: alignments, rows: rows)
    }

    private static func tableAlignments(_ line: String) -> [HorizontalAlignment]? {
        guard line.contains("-") else { return nil }
        let cells = splitRow(line)
        guard !cells.isEmpty else { return nil }
        var alignments: [HorizontalAlignment] = []
        for cell in cells {
            guard !cell.isEmpty, cell.allSatisfy({ $0 == "-" || $0 == ":" }), cell.contains("-") else {
                return nil
            }
            switch (cell.hasPrefix(":"), cell.hasSuffix(":")) {
            case (true, true): alignments.append(.center)
            case (false, true): alignments.append(.trailing)
            default: alignments.append(.leading)
            }
        }
        return alignments
    }

    private static func splitRow(_ line: String) -> [String] {
        var trimmed = line.trimmingCharacters(in: .whitespaces)
        if trimmed.hasPrefix("|") { trimmed.removeFirst() }
        if trimmed.hasSuffix("|") { trimmed.removeLast() }
        return trimmed.components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }
    }

    // MARK: Line matchers

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
        // Closing hashes (`## Title ##`) are decoration, not content.
        var text = line[line.index(after: idx)...].trimmingCharacters(in: .whitespaces)
        while text.hasSuffix("#") { text.removeLast() }
        return (level, text.trimmingCharacters(in: .whitespaces))
    }
}

// MARK: - Inline: HTML normalization + attributed spans

private enum MarkdownInline {
    /// Build the attributed run for one inline string.
    ///
    /// `font` is the block's own font, so an inline code span inside a heading stays heading-sized and one
    /// inside body text stays body-sized — only the *design* changes.
    static func attributed(_ text: String, font: Font) -> AttributedString {
        let source = normalized(text)
        var options = AttributedString.MarkdownParsingOptions(
            interpretedSyntax: .inlineOnlyPreservingWhitespace)
        options.failurePolicy = .returnPartiallyParsedIfPossible
        var attributed = (try? AttributedString(markdown: source, options: options))
            ?? AttributedString(source)

        // Ranges are collected before anything is written: mutating an AttributedString invalidates the
        // run sequence being walked.
        var links: [Range<AttributedString.Index>] = []
        var struck: [Range<AttributedString.Index>] = []
        var code: [Range<AttributedString.Index>] = []
        for run in attributed.runs {
            if run.link != nil { links.append(run.range) }
            guard let intent = run.inlinePresentationIntent else { continue }
            if intent.contains(.strikethrough) { struck.append(run.range) }
            if intent.contains(.code) { code.append(run.range) }
        }

        // The palette is monochrome, so colour can't signal a link — underline is the affordance.
        for range in links { attributed[range].underlineStyle = .single }
        // SwiftUI honours `inlinePresentationIntent` inconsistently; these two are stated outright so
        // `~~struck~~` and `inline code` can't silently render as plain body text.
        for range in struck { attributed[range].strikethroughStyle = .single }
        for range in code {
            attributed[range].font = font.monospaced()
            attributed[range].backgroundColor = Theme.Colors.surfaceSunken
        }
        return attributed
    }

    /// Normalize simple inline HTML into the Markdown that means the same thing, decode entities, and drop
    /// any tag left over — never print one. Text inside backtick spans is copied verbatim, so a code span
    /// that is *about* HTML survives intact.
    static func normalized(_ text: String) -> String {
        guard text.contains("<") || text.contains("&") else { return text }
        var out = ""
        out.reserveCapacity(text.count)
        var index = text.startIndex
        var openLinkHref: String?

        while index < text.endIndex {
            let character = text[index]

            if character == "`" {
                let fence = String(text[index...].prefix { $0 == "`" })
                out += fence
                index = text.index(index, offsetBy: fence.count)
                if let close = text.range(of: fence, range: index..<text.endIndex) {
                    out += text[index..<close.upperBound]
                    index = close.upperBound
                } else {
                    out += text[index...]
                    index = text.endIndex
                }
                continue
            }

            if character == "<", let tag = Tag(text, at: index) {
                switch tag.replacement {
                case .literal(let replacement):
                    out += replacement
                case .linkOpen(let href):
                    out += "["
                    openLinkHref = href
                case .linkClose:
                    if let href = openLinkHref {
                        out += "](\(href))"
                        openLinkHref = nil
                    }
                case .drop:
                    break
                }
                index = tag.end
                continue
            }

            if character == "&", let entity = Entity(text, at: index) {
                out += entity.value
                index = entity.end
                continue
            }

            out.append(character)
            index = text.index(after: index)
        }
        if openLinkHref != nil { out += "]" }   // never leave a dangling bracket open
        return out
    }

    /// Every tag removed and every entity decoded — for `<pre>` bodies, where the content is code and must
    /// not be treated as Markdown.
    static func plainText(fromHTML html: String) -> String {
        var out = ""
        out.reserveCapacity(html.count)
        var index = html.startIndex
        while index < html.endIndex {
            let character = html[index]
            if character == "<", let close = html.range(of: ">", range: index..<html.endIndex) {
                index = close.upperBound
                continue
            }
            if character == "&", let entity = Entity(html, at: index) {
                out += entity.value
                index = entity.end
                continue
            }
            out.append(character)
            index = html.index(after: index)
        }
        return out
    }

    /// `class="language-swift"` → `swift`, so an HTML code block keeps its label.
    static func languageClass(in html: String) -> String? {
        guard let marker = html.range(of: "language-") else { return nil }
        let rest = html[marker.upperBound...]
        let name = rest.prefix { $0.isLetter || $0.isNumber || $0 == "+" || $0 == "#" }
        return name.isEmpty ? nil : String(name)
    }

    // MARK: Tags

    private struct Tag {
        enum Replacement {
            case literal(String)
            case linkOpen(String)
            case linkClose
            case drop
        }

        let end: String.Index
        let replacement: Replacement

        /// Deliberately conservative. A tag is only recognized when it has a known HTML name AND its
        /// attribute text is either empty or actually looks like attributes — otherwise `a < b and c > d`
        /// would be eaten as a `<b>` tag, and `Array<Int>` would lose its type parameter.
        init?(_ text: String, at start: String.Index) {
            var cursor = text.index(after: start)
            guard cursor < text.endIndex else { return nil }
            var isClosing = false
            if text[cursor] == "/" {
                isClosing = true
                cursor = text.index(after: cursor)
            }
            guard cursor < text.endIndex, text[cursor].isLetter else { return nil }

            let nameStart = cursor
            while cursor < text.endIndex, text[cursor].isLetter || text[cursor].isNumber {
                cursor = text.index(after: cursor)
            }
            let name = text[nameStart..<cursor].lowercased()
            guard let role = Tag.roles[name] else { return nil }

            guard let close = text.range(of: ">", range: cursor..<text.endIndex) else { return nil }
            var attributes = String(text[cursor..<close.lowerBound])
            if attributes.hasSuffix("/") { attributes.removeLast() }
            guard attributes.count <= 240, !attributes.contains("<") else { return nil }
            let looksLikeAttributes = attributes.trimmingCharacters(in: .whitespaces).isEmpty
                || attributes.contains("=")
            guard looksLikeAttributes else { return nil }

            end = close.upperBound
            switch role {
            case .delimiter(let delimiter):
                replacement = .literal(delimiter)
            case .lineBreak:
                replacement = .literal("\n")
            case .blockEnd:
                replacement = isClosing ? .literal("\n") : .drop
            case .link:
                if isClosing {
                    replacement = .linkClose
                } else if let href = Tag.href(in: attributes) {
                    replacement = .linkOpen(href)
                } else {
                    replacement = .drop
                }
            case .strip:
                replacement = .drop
            }
        }

        private static func href(in attributes: String) -> String? {
            guard let marker = attributes.range(of: "href", options: .caseInsensitive) else { return nil }
            let rest = attributes[marker.upperBound...].drop { $0 == " " || $0 == "=" }
            guard let quote = rest.first, quote == "\"" || quote == "'" else { return nil }
            let value = rest.dropFirst().prefix { $0 != quote }
            return value.isEmpty ? nil : String(value)
        }

        private enum Role {
            case delimiter(String)
            case lineBreak
            case blockEnd
            case link
            case strip
        }

        private static let roles: [String: Role] = [
            "code": .delimiter("`"), "tt": .delimiter("`"), "kbd": .delimiter("`"),
            "samp": .delimiter("`"), "var": .delimiter("`"),
            "strong": .delimiter("**"), "b": .delimiter("**"),
            "em": .delimiter("*"), "i": .delimiter("*"), "cite": .delimiter("*"),
            "s": .delimiter("~~"), "del": .delimiter("~~"), "strike": .delimiter("~~"),
            "br": .lineBreak, "hr": .lineBreak,
            "p": .blockEnd, "div": .blockEnd, "li": .blockEnd, "tr": .blockEnd,
            "h1": .blockEnd, "h2": .blockEnd, "h3": .blockEnd,
            "h4": .blockEnd, "h5": .blockEnd, "h6": .blockEnd,
            "blockquote": .blockEnd,
            "a": .link,
            "span": .strip, "u": .strip, "mark": .strip, "small": .strip, "sub": .strip,
            "sup": .strip, "ul": .strip, "ol": .strip, "pre": .strip, "abbr": .strip,
            "table": .strip, "thead": .strip, "tbody": .strip, "td": .strip, "th": .strip,
            "img": .strip, "figure": .strip, "figcaption": .strip, "section": .strip,
            "article": .strip, "header": .strip, "footer": .strip, "font": .strip
        ]
    }

    // MARK: Entities

    private struct Entity {
        let end: String.Index
        let value: String

        init?(_ text: String, at start: String.Index) {
            let bodyStart = text.index(after: start)
            guard bodyStart < text.endIndex,
                  let semicolon = text.range(of: ";", range: bodyStart..<text.endIndex) else { return nil }
            let body = String(text[bodyStart..<semicolon.lowerBound])
            guard !body.isEmpty, body.count <= 12,
                  body.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "#" }) else { return nil }

            if body.hasPrefix("#") {
                let digits = body.dropFirst()
                let scalarValue: UInt32? = (digits.first == "x" || digits.first == "X")
                    ? UInt32(digits.dropFirst(), radix: 16)
                    : UInt32(digits)
                guard let raw = scalarValue, let scalar = Unicode.Scalar(raw) else { return nil }
                value = String(Character(scalar))
                end = semicolon.upperBound
                return
            }
            guard let named = Entity.named[body.lowercased()] else { return nil }
            value = named
            end = semicolon.upperBound
        }

        private static let named: [String: String] = [
            "amp": "&", "lt": "<", "gt": ">", "quot": "\"", "apos": "'",
            "nbsp": "\u{00A0}", "hellip": "…", "mdash": "—", "ndash": "–", "minus": "−",
            "times": "×", "divide": "÷", "deg": "°", "middot": "·", "bull": "•",
            "ldquo": "\u{201C}", "rdquo": "\u{201D}", "lsquo": "\u{2018}", "rsquo": "\u{2019}",
            "laquo": "«", "raquo": "»", "copy": "©", "reg": "®", "trade": "™",
            "larr": "←", "rarr": "→", "harr": "↔", "check": "✓"
        ]
    }
}

// MARK: - Views

/// Renders a Markdown string with no third-party dependency.
struct MarkdownText: View {
    let markdown: String
    /// How far the two block types that are *containers in their own right* — fenced code and tables —
    /// are allowed to reach back out of the text column, in points. Sali's prose is inset by one gutter so
    /// the mark has a column; code and tables have no business being the narrowest thing on screen, so
    /// they reclaim exactly that gutter and run to the full screen margin. `0` (the default) keeps every
    /// block in the column, which is what any caller without a gutter wants.
    var bleed: CGFloat = 0

    /// The parse is cached, not recomputed in `body`. A streaming reply flushes ~15×/s and every flush
    /// re-runs the body of EVERY visible bubble — re-lexing each of them from scratch each time. The
    /// cache reduces that to one parse per bubble per actual text change.
    @State private var parsedSource: String?
    @State private var parsedBlocks: [MarkdownBlockItem] = []

    private var blocks: [MarkdownBlockItem] {
        // Only the very first frame parses inline (so nothing ever renders blank); after that the cache
        // is authoritative and `onChange` refreshes it.
        parsedSource == nil ? MarkdownParser.parse(markdown) : parsedBlocks
    }

    var body: some View {
        MarkdownBlocksView(items: blocks, bleed: bleed)
            .textSelection(.enabled)
            .onAppear { reparseIfNeeded() }
            .onChange(of: markdown) { _, _ in reparseIfNeeded() }
    }

    private func reparseIfNeeded() {
        guard parsedSource != markdown else { return }
        parsedBlocks = MarkdownParser.parse(markdown)
        parsedSource = markdown
    }
}

/// The block stack. Split out of `MarkdownText` so a blockquote can render its own contents with the same
/// renderer instead of flattening them into one line of text.
private struct MarkdownBlocksView: View {
    let items: [MarkdownBlockItem]
    var bleed: CGFloat = 0
    /// The prose colour for this stack. A blockquote hands its nested stack the secondary tier, which is
    /// how a quote reads as quoted in a palette with no colour to spend.
    var ink: Color = Theme.Colors.primaryText

    /// Nested list levels step by one micro unit — enough to read as subordinate, never enough to march
    /// the text off the column.
    private let indentStep = Theme.Spacing.l
    /// The marker column. Wide enough for "10." at body size; `minWidth` so it grows rather than clipping
    /// at accessibility sizes.
    private let markerWidth: CGFloat = 20

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.m) {
            ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                blockView(for: item.block, isFirst: index == 0)
            }
        }
        .foregroundStyle(ink)
    }

    @ViewBuilder
    private func blockView(for block: MarkdownBlock, isFirst: Bool) -> some View {
        switch block {
        case let .heading(level, text):
            Text(inline(text, font: headingFont(for: level)))
                .font(headingFont(for: level))
                .foregroundStyle(Theme.Colors.primaryText)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityAddTraits(.isHeader)
                // A heading opens a section, so it takes its air from above — the macro register, since it
                // separates things the reader perceives as different objects. Except at the very top,
                // where that air would only push the first line off the mark it is supposed to sit beside.
                .padding(.top, isFirst ? 0 : (level <= 2 ? Theme.Spacing.s : Theme.Spacing.xs))

        case let .paragraph(text):
            Text(inline(text, font: Theme.Typography.body))
                .font(Theme.Typography.body)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)

        case let .list(items):
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                ForEach(items) { item in listRow(item) }
            }

        case let .blockquote(blocks):
            HStack(alignment: .top, spacing: Theme.Spacing.m) {
                RoundedRectangle(cornerRadius: Theme.Radius.xxs, style: .continuous)
                    .fill(Theme.Colors.borderStrong)
                    .frame(width: 2)
                // Erased on purpose: the renderer is recursive, and a concrete nested type would be
                // infinitely recursive at compile time. Quotes are rare, and the parse above is cached.
                AnyView(MarkdownBlocksView(items: blocks, ink: Theme.Colors.secondaryText))
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .fixedSize(horizontal: false, vertical: true)

        case .horizontalRule:
            Rectangle()
                .fill(Theme.Colors.separator)
                .frame(height: Theme.Stroke.hairline)
                .padding(.vertical, Theme.Spacing.xs)

        case let .codeBlock(code, language):
            CodeBlockView(code: code, language: language)
                .padding(.leading, -bleed)

        case let .table(table):
            tableView(table)
                .padding(.leading, -bleed)
        }
    }

    // MARK: Lists

    private func listRow(_ item: MarkdownListItem) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
            marker(for: item.marker)
                .font(Theme.Typography.body)
                .foregroundStyle(Theme.Colors.secondaryText)
                .frame(minWidth: markerWidth, alignment: markerAlignment(for: item.marker))
            Text(inline(item.text, font: Theme.Typography.body))
                .font(Theme.Typography.body)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.leading, CGFloat(item.depth) * indentStep)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder
    private func marker(for marker: MarkdownListMarker) -> some View {
        switch marker {
        case .bullet:
            Text(verbatim: "•")
        case .number(let value):
            // The list's own numbering, not a re-count: a list that starts at 4 means it.
            Text(verbatim: "\(value).").monospacedDigit()
        case .task(let done):
            Image(systemName: done ? "checkmark.square" : "square")
                .accessibilityLabel(done ? "Done" : "Not done")
        }
    }

    private func markerAlignment(for marker: MarkdownListMarker) -> Alignment {
        if case .number = marker { return .trailing }   // right-aligned so "9." and "10." share an edge
        return .leading
    }

    // MARK: Tables

    private func tableView(_ table: MarkdownTableModel) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            Grid(alignment: .topLeading,
                 horizontalSpacing: Theme.Spacing.l,
                 verticalSpacing: Theme.Spacing.s) {
                GridRow {
                    ForEach(Array(table.headers.enumerated()), id: \.offset) { index, header in
                        Text(inline(header, font: Theme.Typography.subheading))
                            .font(Theme.Typography.subheading)
                            .foregroundStyle(Theme.Colors.primaryText)
                            .fixedSize(horizontal: true, vertical: false)
                            // Applied to the header cell, which is what sets the whole column's alignment.
                            .gridColumnAlignment(table.alignments[index])
                    }
                }
                Rectangle()
                    .fill(Theme.Colors.separator)
                    .frame(height: Theme.Stroke.hairline)
                    .gridCellColumns(max(table.headers.count, 1))
                ForEach(Array(table.rows.enumerated()), id: \.offset) { _, row in
                    GridRow {
                        ForEach(Array(row.enumerated()), id: \.offset) { _, cell in
                            Text(inline(cell, font: Theme.Typography.callout))
                                .font(Theme.Typography.callout)
                                .foregroundStyle(Theme.Colors.secondaryText)
                                // Cells never wrap: a wrapped cell is what breaks the column grid. The
                                // table scrolls instead.
                                .fixedSize(horizontal: true, vertical: false)
                        }
                    }
                }
            }
            .padding(Theme.Spacing.m)
        }
        // Sunken, not `surface`: under the elevation ramp "higher = lighter", so a `surface` fill inside a
        // `background` canvas is LIGHTER than what it sits on and reads as nothing at all. A table is a
        // recess in the page, exactly like an input well.
        .background(Theme.Colors.surfaceSunken)
        .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.s, style: .continuous))
        .saliHairline(radius: Theme.Radius.s, color: Theme.Colors.border)
    }

    // MARK: Type

    /// Three ranks that are actually distinguishable. `h2` and `h3` used to be the same 17pt semibold,
    /// which is no hierarchy at all — and in a monochrome app the type ramp is the only hierarchy there is.
    private func headingFont(for level: Int) -> Font {
        switch level {
        case 1: Theme.Typography.title
        case 2: Theme.Typography.heading
        default: Theme.Typography.subheading
        }
    }

    private func inline(_ text: String, font: Font) -> AttributedString {
        MarkdownInline.attributed(text, font: font)
    }
}
