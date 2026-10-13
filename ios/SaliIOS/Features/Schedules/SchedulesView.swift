import Foundation
import SwiftUI

// Schedules — the recurring work Sali runs on its own clock (cron rules and fixed intervals), surfaced so
// the user can always see what is set to happen and when, rather than being surprised by it.
//
// The list comes from `GET /api/v1/schedules`; creating, pausing/resuming and deleting go through
// `POST /schedules`, `POST /schedules/{name}/enable|disable` and `DELETE /schedules/{name}` — all keyed by
// NAME, and all restricted to the controller role server-side. The screen never insists on its local guess
// of the role: a 403 from any of them quietly retires the controls (§37) instead of failing loudly each
// time. Deletion is irreversible, so it is a swipe plus an explicit confirmation — never a one-tap button
// sitting in the row.
//
// The screen is honest about time. `next_run_at` is a live countdown that keeps ticking on its own, a fire
// time that has already passed reads "Due now" rather than a nonsense future, and a schedule whose last run
// FAILED says so — the previous version matched `last_status` by equality against "error", while the daemon
// actually writes `"error: <reason>"`, so every failure quietly rendered as a neutral grey pill.

// MARK: - Forgiving decode helpers

/// Reads that CANNOT throw. Prefixed `sched` so they stay local to this screen's wire types.
private extension KeyedDecodingContainer {
    func schedString(_ key: Key) -> String? {
        guard let value = try? decodeIfPresent(String.self, forKey: key), !value.isEmpty else { return nil }
        return value
    }
    func schedBool(_ key: Key) -> Bool? {
        guard let value = try? decodeIfPresent(Bool.self, forKey: key) else { return nil }
        return value
    }
    /// A timestamp that degrades to `nil` rather than throwing — see `ScheduleWireDate`.
    func schedDate(_ key: Key) -> Date? {
        if let text = schedString(key) { return ScheduleWireDate.parse(text) }
        if let seconds = try? decodeIfPresent(Double.self, forKey: key) {
            return Date(timeIntervalSince1970: seconds)
        }
        return nil
    }
}

/// Date parsing that DEGRADES A FIELD instead of failing a screen.
///
/// The shared `APIClient.decoder` throws on any stamp it can't read, and `Schedule` carries two of them.
/// That meant a single timezone-naive or space-separated `next_run_at` — the exact shape this app has
/// already been blanked by twice — took the WHOLE schedules list down to an error state, hiding every other
/// schedule because of one bad row. Nothing below throws: a stamp that can't be read costs its own line of
/// metadata and nothing else.
enum ScheduleWireDate {
    static func parse(_ text: String) -> Date? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }

        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = withFraction.date(from: trimmed) { return date }

        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        if let date = plain.date(from: trimmed) { return date }

        // Everything the strict parsers reject: a space instead of the "T", and a timezone-naive stamp
        // (a Postgres `timestamp` rather than `timestamptz`), which the host means as UTC.
        for format in ["yyyy-MM-dd HH:mm:ssXXXXX", "yyyy-MM-dd HH:mm:ss.SSSSSSXXXXX",
                       "yyyy-MM-dd'T'HH:mm:ss.SSSSSS", "yyyy-MM-dd'T'HH:mm:ss",
                       "yyyy-MM-dd HH:mm:ss.SSSSSS", "yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd"] {
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "en_US_POSIX")
            formatter.timeZone = TimeZone(secondsFromGMT: 0)
            formatter.dateFormat = format
            if let date = formatter.date(from: trimmed) { return date }
        }
        return nil
    }
}

// MARK: - Wire shape

/// One entry of `GET /api/v1/schedules` → `ScheduleResponse` (src/sali/api/models.py).
public struct Schedule: Identifiable, Sendable {
    public let id: String
    public let name: String
    public let kind: String              // cron | interval
    public let spec: String              // a cron expression, or an interval like "3600" / "15m"
    public let prompt: String            // what Sali is asked to do when it fires
    public let enabled: Bool
    public let nextRunAt: Date?
    public let lastRunAt: Date?
    public let lastStatus: String?
}

extension Schedule: Decodable {
    private enum Keys: String, CodingKey {
        case id, name, kind, spec, prompt, enabled
        case nextRunAt = "next_run_at", lastRunAt = "last_run_at", lastStatus = "last_status"
    }

    /// Field-by-field, and none of it throws. A schedule the host can only half-describe is still worth
    /// showing — the user needs to know it exists and that it will fire — so every field degrades on its
    /// own rather than taking the row (or, previously, the entire screen) with it.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        name = c.schedString(.name) ?? "Untitled schedule"
        kind = c.schedString(.kind) ?? ""
        spec = c.schedString(.spec) ?? ""
        prompt = c.schedString(.prompt) ?? ""
        // Absent `enabled` means the host didn't say; treating that as "on" would imply work is scheduled
        // that may not be, so the safer reading is paused.
        enabled = c.schedBool(.enabled) ?? false
        nextRunAt = c.schedDate(.nextRunAt)
        lastRunAt = c.schedDate(.lastRunAt)
        lastStatus = c.schedString(.lastStatus)
        // Every mutation is keyed by NAME, so the name is the identity that actually matters; the uuid is
        // used when present purely so two schedules can never collide in a `ForEach`.
        id = c.schedString(.id) ?? name
    }
}

/// The list route's `response_model` is a bare `list[ScheduleResponse]`, so the top level is a JSON array.
/// It is read through a tiny envelope anyway — same forgiving posture as the rest of the app — so a future
/// `{"schedules": [...]}` shape would keep decoding instead of breaking the screen.
///
/// Decoded ELEMENT BY ELEMENT: one malformed schedule is skipped, and the other nine still render.
private struct SchedulesResponse: Decodable {
    let schedules: [Schedule]
    private enum Keys: String, CodingKey { case schedules }

    init(from decoder: Decoder) throws {
        if let keyed = try? decoder.container(keyedBy: Keys.self), keyed.contains(.schedules),
           var nested = try? keyed.nestedUnkeyedContainer(forKey: .schedules) {
            schedules = SchedulesResponse.drain(&nested)
        } else {
            var unkeyed = try decoder.unkeyedContainer()
            schedules = SchedulesResponse.drain(&unkeyed)
        }
    }

    private static func drain(_ unkeyed: inout UnkeyedDecodingContainer) -> [Schedule] {
        var rows: [Schedule] = []
        while !unkeyed.isAtEnd {
            if let row = try? unkeyed.decode(Schedule.self) {
                rows.append(row)
            } else if (try? unkeyed.decode(JSONValue.self)) == nil {
                break   // can't even skip the element — stop rather than spin
            }
        }
        return rows
    }
}

// MARK: - Human-readable cadence

extension Schedule {
    /// A plain-language cadence line derived from `kind` + `spec` ("Every day at 07:30", "Every 15 minutes").
    /// If the spec isn't a shape we can honestly describe, the raw spec is shown verbatim rather than a
    /// guess — see `showsRawSpec`.
    var cadence: String {
        switch kind {
        case "interval": return Self.humanInterval(spec)
        case "cron":     return Self.humanCron(spec)
        default:         return spec.isEmpty ? "Cadence unknown" : spec
        }
    }

    /// True when the cadence line is a translation, so the exact rule is still worth showing underneath.
    var showsRawSpec: Bool { !spec.isEmpty && cadence != spec }

    /// Sooner-first, with "no next run known" last — used to order the active section.
    var nextRunSortKey: Date { nextRunAt ?? .distantFuture }

    /// A copy with `enabled` flipped, so a toggle can reflect immediately while the request is in flight.
    /// Only that one field moves: `next_run_at` is the host's to recompute, never this screen's to invent.
    func settingEnabled(_ isEnabled: Bool) -> Schedule {
        Schedule(id: id, name: name, kind: kind, spec: spec, prompt: prompt, enabled: isEnabled,
                 nextRunAt: nextRunAt, lastRunAt: lastRunAt, lastStatus: lastStatus)
    }

    /// The cadence a typed `when` would produce, for the create sheet to echo back before saving. Five or
    /// more whitespace-separated fields reads as cron; anything else as an interval — the same split the
    /// backend makes. Empty when there's nothing to say.
    static func cadence(forWhen when: String) -> String {
        let trimmed = when.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "" }
        let looksLikeCron = trimmed.split(whereSeparator: \.isWhitespace).count >= 5
        let described = looksLikeCron ? humanCron(trimmed) : humanInterval(trimmed)
        return described == trimmed ? "" : described
    }

    // MARK: interval

    /// "3600" (bare seconds), "15m", "1h30m", "2 hours" → "Every 15 minutes". A spec we can't read is
    /// shown verbatim rather than wrapped in a sentence that would imply we understood it.
    private static func humanInterval(_ spec: String) -> String {
        guard let seconds = intervalSeconds(spec), seconds > 0 else { return spec }
        return "Every \(durationPhrase(seconds))"
    }

    /// Parses an interval spec into seconds. A bare number is seconds (what the backend stores); a
    /// number+unit run (`s`/`m`/`h`/`d`/`w`, long forms included) accumulates. Anything else → nil.
    private static func intervalSeconds(_ raw: String) -> Int? {
        let text = raw.lowercased().filter { !$0.isWhitespace }
        guard !text.isEmpty else { return nil }
        if let plain = Double(text) { return Int(plain.rounded()) }

        var total: Double = 0
        var number = ""
        var unit = ""
        var sawUnit = false

        func flush() -> Bool {
            guard let value = Double(number), let symbol = unit.first else { return false }
            let multiplier: Double
            switch symbol {
            case "s": multiplier = 1
            case "m": multiplier = 60
            case "h": multiplier = 3600
            case "d": multiplier = 86_400
            case "w": multiplier = 604_800
            default: return false
            }
            total += value * multiplier
            number = ""; unit = ""; sawUnit = true
            return true
        }

        for character in text {
            if character.isNumber || character == "." {
                if !unit.isEmpty, !flush() { return nil }
                number.append(character)
            } else if character.isLetter {
                guard !number.isEmpty else { return nil }
                unit.append(character)
            } else {
                return nil
            }
        }
        if !unit.isEmpty, !flush() { return nil }
        guard sawUnit, number.isEmpty else { return nil }
        return Int(total.rounded())
    }

    /// Whole seconds → the largest unit that divides them evenly ("90 minutes", "6 hours", "day").
    private static func durationPhrase(_ seconds: Int) -> String {
        let units: [(seconds: Int, singular: String, plural: String)] = [
            (604_800, "week", "weeks"), (86_400, "day", "days"),
            (3_600, "hour", "hours"), (60, "minute", "minutes")
        ]
        for unit in units where seconds % unit.seconds == 0 {
            let count = seconds / unit.seconds
            return count == 1 ? unit.singular : "\(count) \(unit.plural)"
        }
        return seconds == 1 ? "second" : "\(seconds) seconds"
    }

    // MARK: cron

    /// Translates the common five-field cron shapes. Anything more exotic (month restrictions, mixed
    /// day-of-month + day-of-week, seconds fields) falls back to the raw expression — an approximate
    /// sentence about when Sali will act would be worse than the truth.
    private static func humanCron(_ spec: String) -> String {
        let fields = spec.split(whereSeparator: \.isWhitespace).map(String.init)
        guard fields.count == 5 else { return spec }
        let (minuteField, hourField, dayOfMonth, month, dayOfWeek) =
            (fields[0], fields[1], fields[2], fields[3], fields[4])
        guard month == "*" else { return spec }

        let everyDay = dayOfMonth == "*" && dayOfWeek == "*"

        if minuteField == "*", hourField == "*", everyDay { return "Every minute" }
        if let step = stepValue(minuteField), hourField == "*", everyDay {
            return step == 1 ? "Every minute" : "Every \(step) minutes"
        }
        guard let minute = Int(minuteField), (0...59).contains(minute) else { return spec }
        if hourField == "*", everyDay { return "Every hour at :\(pad(minute))" }
        if let step = stepValue(hourField), everyDay {
            return step == 1 ? "Every hour at :\(pad(minute))" : "Every \(step) hours at :\(pad(minute))"
        }
        guard let hour = Int(hourField), (0...23).contains(hour) else { return spec }
        let time = "\(pad(hour)):\(pad(minute))"

        if everyDay { return "Every day at \(time)" }
        if dayOfMonth == "*", let days = weekdayPhrase(dayOfWeek) { return "\(days) at \(time)" }
        if dayOfWeek == "*", let day = Int(dayOfMonth), (1...31).contains(day) {
            return "Monthly on the \(ordinal(day)) at \(time)"
        }
        return spec
    }

    /// "*/15" → 15. Only a step over the whole range counts; "5-30/5" stays untranslated.
    private static func stepValue(_ field: String) -> Int? {
        guard field.hasPrefix("*/") else { return nil }
        return Int(field.dropFirst(2))
    }

    /// A cron day-of-week field → "Every Monday" / "Weekdays" / "Mon, Wed, Fri". Nil when unparseable.
    private static func weekdayPhrase(_ field: String) -> String? {
        guard field != "*" else { return nil }
        var days: Set<Int> = []
        for part in field.split(separator: ",") {
            let bounds = part.split(separator: "-", omittingEmptySubsequences: false)
            switch bounds.count {
            case 1:
                guard let day = weekdayIndex(String(bounds[0])) else { return nil }
                days.insert(day)
            case 2:
                guard let start = weekdayIndex(String(bounds[0])),
                      let end = weekdayIndex(String(bounds[1])), start <= end else { return nil }
                days.formUnion(start...end)
            default:
                return nil
            }
        }
        guard !days.isEmpty else { return nil }
        if days == [1, 2, 3, 4, 5] { return "Weekdays" }
        if days == [0, 6] { return "Weekends" }

        let formatter = DateFormatter()
        let ordered = days.sorted()
        if ordered.count == 1 { return "Every \(formatter.weekdaySymbols[ordered[0]])" }
        return ordered.map { formatter.shortWeekdaySymbols[$0] }.joined(separator: ", ")
    }

    /// Cron numbering (0 or 7 = Sunday) or a three-letter name → a 0-based, Sunday-first index.
    private static func weekdayIndex(_ token: String) -> Int? {
        if let number = Int(token) {
            guard (0...7).contains(number) else { return nil }
            return number == 7 ? 0 : number
        }
        let names = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
        return names.firstIndex(of: token.lowercased().prefix(3).description)
    }

    private static func pad(_ value: Int) -> String { String(format: "%02d", value) }

    private static func ordinal(_ value: Int) -> String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .ordinal
        return formatter.string(from: NSNumber(value: value)) ?? "\(value)"
    }
}

// MARK: - Outcome of the last run

/// How the previous fire went, read from `last_status`.
///
/// **This was miswired.** `SchedulerDaemon.tick` writes the literal `"ok"` on success and
/// `"error: <exception>"` on failure (src/sali/scheduler/daemon.py), but the screen matched the status by
/// EQUALITY against `"error"` — which `"error: connection refused"` never equals. Every failed schedule
/// therefore fell through to the neutral default and rendered as a grey pill reading "Last run: error:
/// connection refused": the one state on this screen that genuinely wants the user, shown as if it were
/// nothing. Matching is by prefix now, and the reason is split out of the status so it can be read.
enum ScheduleOutcome: Equatable {
    case neverRun
    case succeeded
    case failed(String?)
    case skipped(String)
    case unknown(String)

    init(_ status: String?, hasRun: Bool) {
        guard let raw = status?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty else {
            self = hasRun ? .unknown("no status recorded") : .neverRun
            return
        }
        let lower = raw.lowercased()
        if lower.hasPrefix("ok") || ["success", "succeeded", "done", "completed"].contains(lower) {
            self = .succeeded
        } else if lower.hasPrefix("error") || lower.hasPrefix("fail") || lower.hasPrefix("exception")
                    || lower.hasPrefix("timeout") || lower.hasPrefix("timed out") {
            self = .failed(ScheduleOutcome.reason(from: raw))
        } else if lower.hasPrefix("skip") || lower.hasPrefix("miss") {
            self = .skipped(raw)
        } else {
            self = .unknown(raw)
        }
    }

    /// `"error: connection refused"` → `"connection refused"`. A status that is only the word "error"
    /// carries no reason, and inventing one would be worse than saying nothing.
    private static func reason(from raw: String) -> String? {
        guard let colon = raw.firstIndex(of: ":") else { return nil }
        let tail = raw[raw.index(after: colon)...].trimmingCharacters(in: .whitespacesAndNewlines)
        return tail.isEmpty ? nil : tail
    }

    /// Only a run that FAILED or was skipped earns a pill; "it worked" is the expected case and folds into
    /// the metadata line instead of adding a second badge to every healthy row (item 12).
    var pill: (label: String, color: Color)? {
        switch self {
        case .neverRun, .succeeded:  return nil
        case .failed:                return ("Last run failed", Theme.Colors.danger)
        case .skipped:               return ("Last run skipped", Theme.Colors.warn)
        case .unknown:               return ("Last run unclear", Theme.Colors.idle)
        }
    }

    /// The machine's own words, shown verbatim in mono beneath the pill — never paraphrased.
    var detail: String? {
        switch self {
        case .failed(let reason):  return reason
        case .skipped(let raw):    return raw
        case .unknown(let raw):    return raw == "no status recorded" ? nil : raw
        case .neverRun, .succeeded: return nil
        }
    }
}

// MARK: - View model

@MainActor
final class SchedulesViewModel: ObservableObject {
    @Published var state: Loadable<[Schedule]> = .idle

    /// Set when the server answers 403 to a mutation: this device isn't a controller, whatever the locally
    /// cached role claims. The controls retire quietly instead of every tap raising the same alert.
    @Published private(set) var permissionDenied = false

    @Published var workingScheduleName: String?
    @Published var actionError: String?

    /// Set when a background refresh fails while a list is already on screen. The list stays; only the
    /// footer changes (item 2 — a transient error keeps last-known content, and says so).
    @Published private(set) var staleReason: String?

    @Published private(set) var isSaving = false
    @Published var createError: String?

    /// Matches the scheduler daemon's own poll (`SchedulerDaemon.poll_s = 30`), so a schedule that fires
    /// while this screen is open shows its new `last_run_at` / `next_run_at` without a manual pull.
    private let pollInterval: UInt64 = 30_000_000_000

    /// Enabled first (soonest run first), then the paused ones by name — what's about to happen leads.
    var active: [Schedule] {
        (state.value ?? []).filter(\.enabled)
            .sorted { ($0.nextRunSortKey, $0.name) < ($1.nextRunSortKey, $1.name) }
    }

    var paused: [Schedule] {
        (state.value ?? []).filter { !$0.enabled }
            .sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
    }

    /// Loads once, then keeps the list honest on a modest cadence for as long as the caller's `.task` is
    /// alive. SwiftUI cancels it when the screen goes away, which stops the loop.
    func run(api: APIClient) async {
        // Unconditional: `load` only shows the skeleton when there is nothing to keep, so re-entering the
        // screen refreshes it immediately instead of showing a list that could be half an hour old.
        await load(api: api)
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: pollInterval)
            if Task.isCancelled { break }
            await load(api: api)
        }
    }

    func load(api: APIClient) async {
        if state.value == nil { state = .loading }
        do {
            let response: SchedulesResponse = try await api.get("schedules")
            state = .loaded(response.schedules)
            staleReason = nil
        } catch {
            // A transient failure on refresh keeps the last-known list rather than blanking the screen.
            if state.value == nil {
                state = .failed((error as? APIError)?.errorDescription ?? "Couldn't load schedules.")
            } else {
                staleReason = (error as? APIError)?.errorDescription ?? "Couldn't reach the host."
            }
        }
    }

    /// A schedule fired on the host, so `last_run_at` / `last_status` / `next_run_at` all just moved.
    /// `SchedulerDaemon._emit_fired` writes `schedule.fired`; routines emit `routine.completed|failed`.
    func handleLiveEvent(_ event: SaliEvent, api: APIClient) {
        var relevant = event.rawType.hasPrefix("schedule.")
        // Routines are classified into the `.routine` family rather than a top-level `EventType`, so the
        // family is what has to be matched — `routine.completed` / `routine.failed`.
        if case .other(let family) = event.type, family.group == .routine { relevant = true }
        guard relevant else { return }
        Task { await load(api: api) }
    }

    // MARK: mutations

    /// Pause / resume, keyed by name. The row flips immediately so the tap feels answered, then the list is
    /// re-fetched so `next_run_at` and the rest come from the host. A failure puts the row back exactly as
    /// it was and says why.
    func setEnabled(_ schedule: Schedule, to isEnabled: Bool, api: APIClient) async {
        applyLocally(schedule.settingEnabled(isEnabled))
        workingScheduleName = schedule.name
        defer { workingScheduleName = nil }
        do {
            try await api.postVoid("schedules/\(schedule.name)/\(isEnabled ? "enable" : "disable")")
            await load(api: api)
        } catch {
            applyLocally(schedule)   // revert the optimistic flip — only this row, nothing else
            report(error, fallback: isEnabled ? "Couldn't resume that schedule."
                                              : "Couldn't pause that schedule.")
            if Self.isNotFound(error) { await load(api: api) }
        }
    }

    /// Irreversible, so it only ever runs behind an explicit confirmation in the view.
    func delete(_ schedule: Schedule, api: APIClient) async {
        workingScheduleName = schedule.name
        defer { workingScheduleName = nil }
        do {
            try await api.deleteVoid("schedules/\(schedule.name)")
            if var list = state.value {
                list.removeAll { $0.id == schedule.id }
                state = .loaded(list)
            }
            await load(api: api)
        } catch {
            report(error, fallback: "Couldn't delete that schedule.")
            if Self.isNotFound(error) { await load(api: api) }
        }
    }

    /// Creates a schedule. NOTE the backend field is `when` (a cron expression *or* an interval), not the
    /// `spec` the read model returns. Returns true when the sheet should close.
    ///
    /// Deliberately `postVoid` + reload rather than decoding the reply. The route DOES answer with a full
    /// `ScheduleResponse`, but decoding it made a successful create *look* like a failure whenever one
    /// field of the reply was unexpected — the schedule existed on the host, and the sheet stayed open
    /// insisting it hadn't been made. `postVoid` never parses the body, so the only thing that can fail
    /// here is the request itself, and the truth comes from the reload.
    func create(name: String, when: String, prompt: String, api: APIClient) async -> Bool {
        isSaving = true
        createError = nil
        defer { isSaving = false }
        do {
            try await api.postVoid("schedules", json: [
                "name": name.trimmingCharacters(in: .whitespacesAndNewlines),
                "when": when.trimmingCharacters(in: .whitespacesAndNewlines),
                "prompt": prompt.trimmingCharacters(in: .whitespacesAndNewlines)
            ])
            await load(api: api)
            return true
        } catch {
            createError = Self.createFailureMessage(error)
            if let apiError = error as? APIError, case .forbidden = apiError { permissionDenied = true }
            return false
        }
    }

    /// The backend rejects an unusable `when` with a 400 whose body explains why (`cron.parse_when` raises
    /// `ScheduleError`), but the shared client collapses every other-status failure to "request failed".
    /// Rather than showing the user "Server error (400): request failed", say the thing that is actually
    /// true about a 400 on this route: the expression was the problem.
    private static func createFailureMessage(_ error: Error) -> String {
        guard let apiError = error as? APIError else { return "Couldn't create that schedule." }
        switch apiError {
        case .forbidden:
            return "Only a controller device can create schedules."
        case .conflict:
            return "A schedule with that name already exists."
        case .server(let code, _) where code == 400 || code == 422:
            return "Sali couldn't read that timing. Use a 5-field cron expression (\"30 7 * * *\") "
                 + "or an interval (\"15m\", \"2h\", \"3600\")."
        default:
            return apiError.errorDescription ?? "Couldn't create that schedule."
        }
    }

    /// Replace one row in place, leaving the rest of the list (and any concurrent refresh) untouched.
    private func applyLocally(_ schedule: Schedule) {
        guard var list = state.value,
              let index = list.firstIndex(where: { $0.id == schedule.id }) else { return }
        list[index] = schedule
        state = .loaded(list)
    }

    /// Turns a failed mutation into the right response: a 403 retires the controls silently (the server is
    /// the authority on role, not the token we cached), a 404 says the schedule is gone, anything else is
    /// surfaced once in an alert.
    private func report(_ error: Error, fallback: String) {
        guard let apiError = error as? APIError else { actionError = fallback; return }
        switch apiError {
        case .forbidden:
            permissionDenied = true
        case .notFound:
            actionError = "That schedule no longer exists on the host."
        default:
            actionError = apiError.errorDescription ?? fallback
        }
    }

    /// The schedule was deleted or renamed on the host — the list we're showing is stale, so re-fetch.
    private static func isNotFound(_ error: Error) -> Bool {
        guard let apiError = error as? APIError, case .notFound = apiError else { return false }
        return true
    }
}

// MARK: - View

/// Pushed as a `NavigationLink` destination from `MoreView`, so — like Learning and Messages — this shares
/// the navigation stack already on screen instead of opening its own.
struct SchedulesView: View {
    @EnvironmentObject private var appState: AppState
    @StateObject private var viewModel = SchedulesViewModel()

    @State private var showCreateSheet = false
    @State private var pendingDeletion: Schedule?

    /// Both authorities have to agree: the device's own role, and the server's answer to a real attempt.
    private var canManage: Bool { appState.role.canControl && !viewModel.permissionDenied }

    var body: some View {
        content
            .navigationTitle("Schedules")
            .toolbar {
                if canManage {
                    ToolbarItem(placement: .primaryAction) {
                        Button { showCreateSheet = true } label: {
                            Label("New schedule", systemImage: "plus")
                        }
                    }
                }
            }
            .task { await viewModel.run(api: appState.api) }
            .refreshable { await viewModel.load(api: appState.api) }
            .onChange(of: appState.latestEvent?.id) { _, _ in
                if let event = appState.latestEvent {
                    viewModel.handleLiveEvent(event, api: appState.api)
                }
            }
            .sheet(isPresented: $showCreateSheet, onDismiss: { viewModel.createError = nil }) {
                CreateScheduleSheet(viewModel: viewModel, api: appState.api)
            }
            .confirmationDialog(Text("Delete this schedule?"), isPresented: deleteConfirmationBinding,
                                titleVisibility: .visible, presenting: pendingDeletion) { schedule in
                Button("Delete schedule", role: .destructive) {
                    pendingDeletion = nil
                    Task { await viewModel.delete(schedule, api: appState.api) }
                }
                Button("Cancel", role: .cancel) { pendingDeletion = nil }
            } message: { schedule in
                Text("“\(schedule.name)” will be removed on the Sali host and won't run again. "
                     + "This can't be undone from here.")
            }
            .alert("That didn't go through", isPresented: Binding(
                get: { viewModel.actionError != nil },
                set: { if !$0 { viewModel.actionError = nil } }
            )) {
                Button("OK", role: .cancel) { viewModel.actionError = nil }
            } message: {
                Text(viewModel.actionError ?? "")
            }
    }

    private var deleteConfirmationBinding: Binding<Bool> {
        Binding(get: { pendingDeletion != nil }, set: { if !$0 { pendingDeletion = nil } })
    }

    @ViewBuilder private var content: some View {
        switch viewModel.state {
        case .idle, .loading:
            // Shaped like the real rows — title, pill, three lines of detail — so the content settles into
            // the layout already on screen instead of replacing a spinner (item 1).
            SaliSkeletonList(rows: 4, lines: 3)
        case .failed(let message):
            ErrorStateView(message) { Task { await viewModel.load(api: appState.api) } }
        case .loaded(let schedules):
            if schedules.isEmpty {
                EmptyStateView(
                    icon: "calendar",
                    title: "No schedules yet",
                    message: canManage
                        ? "Recurring work Sali runs on its own clock lives here. Add one with ＋ — give it a name, when it should run, and what to do."
                        : """
                        Recurring work Sali runs on its own clock will appear here — with what it does, when \
                        it next runs, and how the last run went.
                        """
                )
            } else {
                scheduleList
            }
        }
    }

    private var scheduleList: some View {
        List {
            if !viewModel.active.isEmpty {
                Section {
                    ForEach(viewModel.active) { schedule in row(schedule) }
                } header: {
                    SectionHeader("Active", subtitle: "Soonest first")
                } footer: {
                    SectionFooter(guidance, detail: activeDetail)
                }
            }

            if !viewModel.paused.isEmpty {
                Section {
                    ForEach(viewModel.paused) { schedule in row(schedule) }
                } header: {
                    SectionHeader("Paused", subtitle: "Will not run until resumed")
                } footer: {
                    if viewModel.active.isEmpty {
                        SectionFooter(guidance, detail: "\(viewModel.paused.count) paused")
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
        .saliList()
    }

    /// One quiet line, in the first section's footer — an observer is told once why the controls aren't
    /// there, rather than discovering it one failed tap at a time. A refresh that failed says so here too,
    /// which is what earns the right to keep showing the previous list.
    private var guidance: String {
        if let staleReason = viewModel.staleReason {
            return "\(staleReason) Showing the last schedules Sali sent."
        }
        if viewModel.permissionDenied {
            return "The host declined that change — this device isn't a controller, so the controls are "
                 + "hidden."
        }
        return canManage
            ? "Swipe a schedule left to delete it — that can't be undone."
            : "Read-only on this device. Only a controller can add, pause, or delete schedules."
    }

    private var activeDetail: String? {
        let count = viewModel.active.count
        return count == 1 ? "1 active" : "\(count) active"
    }

    @ViewBuilder private func row(_ schedule: Schedule) -> some View {
        ScheduleRow(
            schedule: schedule,
            canManage: canManage,
            isWorking: viewModel.workingScheduleName == schedule.name
        ) { isEnabled in
            Task { await viewModel.setEnabled(schedule, to: isEnabled, api: appState.api) }
        }
        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
            if canManage {
                Button(role: .destructive) {
                    pendingDeletion = schedule
                } label: {
                    Label("Delete", systemImage: "trash")
                }
            }
        }
    }
}

// MARK: - Row

/// One schedule, read top to bottom: what it is called, when it runs, the exact rule behind that, what it
/// asks Sali to do, and only then the timing metadata. Every rank is a different size AND a different ink —
/// the old row set five different things in `caption`, which is the same as setting none of them.
private struct ScheduleRow: View {
    let schedule: Schedule
    let canManage: Bool
    let isWorking: Bool
    let onSetEnabled: (Bool) -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var outcome: ScheduleOutcome {
        ScheduleOutcome(schedule.lastStatus, hasRun: schedule.lastRunAt != nil)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack(alignment: .top, spacing: Theme.Spacing.s) {
                Text(schedule.name)
                    .font(Theme.Typography.heading)
                    .foregroundStyle(Theme.Colors.primaryText)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                    // This one element speaks for the whole row; everything below it is hidden from
                    // VoiceOver rather than repeated, and the toggle stays a separate, operable control.
                    .accessibilityLabel(accessibilityText)
                Spacer(minLength: Theme.Spacing.s)
                control
            }

            details
        }
        .padding(.vertical, Theme.Spacing.xs)
        // `.contain`, NOT `.combine`. Combining flattened the row into one static label and took the
        // pause/resume switch out of the accessibility tree entirely, so the only control on this screen
        // could not be operated by VoiceOver at all.
        .accessibilityElement(children: .contain)
    }

    /// Everything under the title. Hidden from VoiceOver because `accessibilityText` already says all of
    /// it, in a better order, on the title element.
    private var details: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            // The cadence is what the user actually scans this list for, so it leads the detail.
            Text(schedule.cadence)
                .font(Theme.Typography.subheading)
                .foregroundStyle(Theme.Colors.secondaryText)
                .fixedSize(horizontal: false, vertical: true)

            // The exact rule, only when the line above it is a translation of something else.
            if schedule.showsRawSpec {
                Text(schedule.spec)
                    .font(Theme.Typography.monoSmall)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .lineLimit(1)
            }

            if !schedule.prompt.isEmpty {
                Text(schedule.prompt)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }

            timing

            // A pill ONLY when the last run needs the user. A healthy schedule stays silent (item 12).
            if let pill = outcome.pill {
                VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                    StatusPill(pill.label, color: pill.color)
                    if let detail = outcome.detail {
                        Text(detail)
                            .font(Theme.Typography.monoSmall)
                            .foregroundStyle(Theme.Colors.tertiaryText)
                            .lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.top, Theme.Spacing.xs)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityHidden(true)
    }

    /// The toggle keeps its slot while a request is in flight — the spinner used to REPLACE it, and a 51pt
    /// control swapping for a 20pt one made the whole row jump on every tap. It is disabled instead, with a
    /// small progress ring beside it, so nothing moves.
    @ViewBuilder private var control: some View {
        if canManage {
            HStack(spacing: Theme.Spacing.xs) {
                if isWorking { ProgressView().controlSize(.small) }
                Toggle("", isOn: Binding(get: { schedule.enabled }, set: onSetEnabled))
                    .labelsHidden()
                    .tint(Theme.Colors.accent)
                    .disabled(isWorking)
                    .opacity(isWorking ? 0.5 : 1)
                    .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick), value: isWorking)
            }
            // A switch is 51×31; the 44pt minimum comes from the frame, and `contentShape` makes the extra
            // air hit-testable instead of decorative.
            .frame(minHeight: 44)
            .contentShape(Rectangle())
            .accessibilityLabel(schedule.enabled ? "Pause this schedule" : "Resume this schedule")
        } else {
            // No control to offer an observer, so state is stated instead — in words, not just colour.
            StatusPill(schedule.enabled ? "Active" : "Paused",
                       color: schedule.enabled ? Theme.Colors.secondaryText : Theme.Colors.idle)
                .frame(minHeight: 44)
        }
    }

    /// Timestamps and counts — `metadata` in `tertiaryText`, the tier the system reserves for exactly this.
    private var timing: some View {
        HStack(spacing: Theme.Spacing.xs) {
            nextRunText
            Text("·")
            Text(lastRunText)
            Spacer(minLength: 0)
        }
        .font(Theme.Typography.metadata)
        .foregroundStyle(Theme.Colors.tertiaryText)
        .lineLimit(1)
    }

    /// A LIVE countdown. `saliRelative` is computed once at render, so the old row said "Next 4 min" long
    /// after those four minutes were gone; `Text(_:style:.relative)` keeps counting on its own. A fire time
    /// that has already passed reads "Due now" rather than a future that isn't one.
    @ViewBuilder private var nextRunText: some View {
        if !schedule.enabled {
            Text("Paused")
        } else if let nextRunAt = schedule.nextRunAt {
            if nextRunAt.timeIntervalSinceNow <= 0 {
                Text("Due now")
            } else {
                Text("Next in ") + Text(nextRunAt, style: .relative)
            }
        } else {
            Text("Next run unknown")
        }
    }

    /// Degrades by FIELD: an unreadable `last_run_at` costs the *time*, not the fact that it ran. Saying
    /// "never run" about a schedule the host recorded a status for would be a lie told by a parser.
    private var lastRunText: String {
        if let lastRunAt = schedule.lastRunAt { return "ran \(lastRunAt.saliRelative)" }
        return outcome == .neverRun ? "never run" : "ran, time unknown"
    }

    private var accessibilityText: String {
        var parts = [schedule.name, schedule.cadence]
        if !schedule.prompt.isEmpty { parts.append(schedule.prompt) }
        if !schedule.enabled {
            parts.append("Paused")
        } else if let nextRunAt = schedule.nextRunAt {
            parts.append(nextRunAt.timeIntervalSinceNow <= 0
                         ? "Due now" : "Next run \(nextRunAt.saliRelative)")
        } else {
            parts.append("Next run unknown")
        }
        if let lastRunAt = schedule.lastRunAt { parts.append("Last ran \(lastRunAt.saliRelative)") }
        if let pill = outcome.pill { parts.append(pill.label) }
        if let detail = outcome.detail { parts.append(detail) }
        return parts.joined(separator: ". ")
    }
}

// MARK: - Create

/// Name, when, prompt — the exact three fields `POST /api/v1/schedules` takes. Save stays disabled until all
/// three are filled in, and the cadence is echoed back in plain language first so nobody schedules "*/5" by
/// accident thinking it means five hours.
private struct CreateScheduleSheet: View {
    @ObservedObject var viewModel: SchedulesViewModel
    let api: APIClient
    @Environment(\.dismiss) private var dismiss

    @State private var name = ""
    @State private var when = ""
    @State private var prompt = ""

    private var isValid: Bool {
        [name, when, prompt].allSatisfy {
            !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
    }

    private var cadencePreview: String { Schedule.cadence(forWhen: when) }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Name", text: $name)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .font(Theme.Typography.body)
                        .frame(minHeight: 44)
                    TextField("When", text: $when)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .font(Theme.Typography.mono)
                        .frame(minHeight: 44)
                } header: {
                    SectionHeader("Schedule", subtitle: "What to call it, and how often it runs")
                } footer: {
                    whenFooter
                }

                Section {
                    TextField("What should Sali do?", text: $prompt, axis: .vertical)
                        .font(Theme.Typography.body)
                        .lineLimit(3...6)
                } header: {
                    SectionHeader("Prompt", subtitle: "What Sali is asked to do when it fires")
                } footer: {
                    SectionFooter("It runs as a full turn, exactly as if you had typed it in chat.")
                }

                if let error = viewModel.createError {
                    Section {
                        Text(error)
                            .font(Theme.Typography.footnote)
                            .foregroundStyle(Theme.Colors.danger)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    }
                }
            }
            .saliList()
            .navigationTitle("New schedule")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    if viewModel.isSaving {
                        ProgressView().controlSize(.small)
                    } else {
                        Button("Save") {
                            Task {
                                if await viewModel.create(name: name, when: when, prompt: prompt, api: api) {
                                    dismiss()
                                }
                            }
                        }
                        .disabled(!isValid)
                    }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }

    /// Says what the field takes, and — the moment the typed rule is one we can read — what it will
    /// actually mean, so "*/5" is confirmed as five MINUTES before it is saved rather than after.
    private var whenFooter: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            Text("A 5-field cron expression (\"30 7 * * *\") or an interval (\"15m\", \"2h\", \"3600\").")
                .font(Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.secondaryText)
                .fixedSize(horizontal: false, vertical: true)
            if !cadencePreview.isEmpty {
                Text("Runs \(cadencePreview.lowercasedFirstWord).")
                    .font(Theme.Typography.footnote.weight(.medium))
                    .foregroundStyle(Theme.Colors.primaryText)
                    .fixedSize(horizontal: false, vertical: true)
            } else if !when.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                Text("Sali will check this expression when you save it.")
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, Theme.Spacing.s)
        .padding(.bottom, Theme.Spacing.l)
        .textCase(nil)
        .listRowInsets(EdgeInsets(top: 0, leading: Theme.Spacing.listMargin,
                                  bottom: 0, trailing: Theme.Spacing.listMargin))
        .listRowBackground(Color.clear)
        .listRowSeparator(.hidden)
    }
}

private extension String {
    /// "Every day at 07:30" → "every day at 07:30", so it reads as part of a sentence.
    var lowercasedFirstWord: String {
        guard let first else { return self }
        return first.lowercased() + dropFirst()
    }
}

#Preview {
    NavigationStack {
        SchedulesView().environmentObject(AppState())
    }
}
