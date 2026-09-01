import Foundation
import Security

/// A tiny, focused Keychain wrapper. Device credentials (access + refresh tokens) live ONLY here — never in
/// UserDefaults, never in a file, never logged (§24). Items are stored `WhenUnlockedThisDeviceOnly` so they
/// don't sync to iCloud or restore onto another device.
public enum Keychain {
    public enum KeychainError: Error { case unexpectedStatus(OSStatus) }

    public static func set(_ value: String, for key: String) throws {
        let data = Data(value.utf8)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: key,
        ]
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
        ]
        let status = SecItemCopyMatching(query as CFDictionary, nil)
        if status == errSecSuccess {
            let s = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
            guard s == errSecSuccess else { throw KeychainError.unexpectedStatus(s) }
        } else if status == errSecItemNotFound {
            let s = SecItemAdd(query.merging(attributes) { $1 } as CFDictionary, nil)
            guard s == errSecSuccess else { throw KeychainError.unexpectedStatus(s) }
        } else {
            throw KeychainError.unexpectedStatus(status)
        }
    }

    public static func get(_ key: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: key,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    public static func delete(_ key: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: key,
        ]
        SecItemDelete(query as CFDictionary)
    }
}
