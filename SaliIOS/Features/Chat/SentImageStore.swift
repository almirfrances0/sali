import Foundation

// Durable, on-device storage for the photos you send Sali.
//
// Why this file exists at all: `GET /api/v1/conversation` returns TEXT ONLY, and there is no endpoint
// that hands a conversation file back. The durable user turn for a photo is stored server-side as
// `caption + "\n\n[📷 Photo]"`, so history can tell us THAT a photo was sent, never what it looked like.
// `FileAttachment.imageData` held the JPEG in memory, so the thumbnail survived exactly as long as the
// process did: kill the app, come back, and the bubble was a caption with a marker glued to it.
//
// The bytes therefore have to live on the device. This mirrors `NotificationStore` (Features/Notifications)
// deliberately: a JSON index in Application Support, the payloads beside it on disk, bounded so a chatty
// month can't fill the phone.

/// One photo this device sent into the conversation.
public struct SentImageRecord: Codable, Equatable, Identifiable {
    /// Identity, and the stem of the file on disk.
    public let id: String
    /// The upload filename (`photo-<uuid>.jpg`), minted once per pick and reused verbatim by a retry —
    /// which makes it the de-duplication key: retrying a failed send updates this record in place instead
    /// of appending a second one and shifting every later match by one.
    public let filename: String
    /// Whatever was typed alongside the photo. Kept for provenance; the transcript prefers the durable
    /// server text, which is authoritative.
    public var caption: String
    /// When the photo was actually sent. The order this file is read back in.
    public let sentAt: Date
    public var byteCount: Int

    public init(id: String, filename: String, caption: String, sentAt: Date, byteCount: Int) {
        self.id = id
        self.filename = filename
        self.caption = caption
        self.sentAt = sentAt
        self.byteCount = byteCount
    }
}

/// A bounded, file-backed cache of sent photos, ordered oldest → newest.
///
/// Bounds are both a count and a byte ceiling, because either one alone is the wrong limit: 40 tiny
/// screenshots are nothing, 40 full-frame photos are not. Whichever is hit first prunes the OLDEST
/// records, which is also what makes restore correct — the cache and the conversation window both keep
/// their most recent entries, so they are matched from the newest end backwards.
@MainActor
final class SentImageStore {
    /// Newest last. The order `restore` walks.
    private(set) var records: [SentImageRecord] = []

    /// Roughly a month of ordinary use. Uploads are downscaled to a 1600px long edge at JPEG 0.8
    /// (~200–400 KB each), so 40 images is well inside the byte ceiling in practice.
    private let maxImages = 40
    private let maxTotalBytes = 24 * 1024 * 1024

    private let directory: URL
    private let indexURL: URL

    init() {
        let base = (try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                 appropriateFor: nil, create: true))
            ?? FileManager.default.temporaryDirectory
        directory = base.appendingPathComponent("sali-sent-images", isDirectory: true)
        indexURL = directory.appendingPathComponent("index.json")
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        load()
    }

    // MARK: - Reading

    func fileURL(for record: SentImageRecord) -> URL {
        directory.appendingPathComponent("\(record.id).jpg")
    }

    /// Memory-mapped, not read: a restored transcript can carry several photos at once, and there is no
    /// reason for all of them to sit resident just because they are on screen somewhere above the fold.
    func data(for record: SentImageRecord) -> Data? {
        try? Data(contentsOf: fileURL(for: record), options: .mappedIfSafe)
    }

    // MARK: - Writing

    /// Remember a photo that has actually been delivered.
    ///
    /// Called only once the turn was accepted, so the cache and the server's history stay in step: a send
    /// that never reached Sali leaves no `[📷 Photo]` marker, and must therefore leave no record either —
    /// an extra record would shift every later match by one.
    @discardableResult
    func record(filename: String, caption: String, data: Data, sentAt: Date = Date()) -> SentImageRecord? {
        guard !data.isEmpty, !filename.isEmpty else { return nil }

        // A retry re-sends the same filename. Update in place — one photo, one record, one marker.
        if let index = records.firstIndex(where: { $0.filename == filename }) {
            var existing = records[index]
            existing.caption = caption
            existing.byteCount = data.count
            guard write(data, to: fileURL(for: existing)) else { return nil }
            records[index] = existing
            persist()
            return existing
        }

        let new = SentImageRecord(id: UUID().uuidString, filename: filename, caption: caption,
                                  sentAt: sentAt, byteCount: data.count)
        guard write(data, to: fileURL(for: new)) else { return nil }
        records.append(new)
        prune()
        persist()
        return new
    }

    /// Sign-out. The photos are the person's, not the app's — they go with the session.
    func clear() {
        for record in records { try? FileManager.default.removeItem(at: fileURL(for: record)) }
        records.removeAll()
        try? FileManager.default.removeItem(at: indexURL)
    }

    // MARK: - Disk

    private func write(_ data: Data, to url: URL) -> Bool {
        do {
            try data.write(to: url, options: .atomic)
            return true
        } catch {
            return false
        }
    }

    private func load() {
        guard let data = try? Data(contentsOf: indexURL),
              let decoded = try? JSONDecoder().decode([SentImageRecord].self, from: data) else { return }
        // An index entry whose bytes are gone (a restore from backup, a manual purge) is not a photo —
        // keeping it would silently shift positional matching by one. Reconcile with the disk first.
        let surviving = decoded
            .filter { FileManager.default.fileExists(atPath: fileURL(for: $0).path) }
            .sorted { $0.sentAt < $1.sentAt }
        records = surviving
        if surviving.count != decoded.count { persist() }
    }

    private func persist() {
        guard let data = try? JSONEncoder().encode(records) else { return }
        try? data.write(to: indexURL, options: .atomic)
    }

    /// Oldest out first, by count and then by total bytes. Both bounds delete the file too — an index that
    /// forgets a payload is just a leak with extra steps.
    private func prune() {
        records.sort { $0.sentAt < $1.sentAt }

        while records.count > maxImages {
            let dropped = records.removeFirst()
            try? FileManager.default.removeItem(at: fileURL(for: dropped))
        }

        var total = records.reduce(0) { $0 + $1.byteCount }
        while total > maxTotalBytes, records.count > 1 {
            let dropped = records.removeFirst()
            total -= dropped.byteCount
            try? FileManager.default.removeItem(at: fileURL(for: dropped))
        }
    }
}
