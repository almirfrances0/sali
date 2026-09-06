// swift-tools-version: 5.9
//
// SaliIOS — the iPhone control center for Sali (Prompt 13).
//
// This SPM package builds the app's sources as a library so the logic layers (Core, DesignSystem, Models,
// Features) can be developed and, on macOS, unit-tested. The final app target is created in Xcode on macOS
// (see docs/XCODE_HANDOFF.md) — this package intentionally does NOT define an executable/app target, and
// nothing here needs to compile or sign on the Kali host.
//
// Pure SwiftUI + Foundation + async/await. No third-party dependencies (Markdown is rendered in-app), so
// there is nothing to resolve offline — open it in Xcode and add an App target that depends on this library,
// or drag the folders into a new iOS App project.

import PackageDescription

let package = Package(
    name: "SaliIOS",
    platforms: [
        .iOS(.v17),
        .macOS(.v14),
    ],
    products: [
        .library(name: "SaliIOS", targets: ["SaliIOS"]),
    ],
    targets: [
        .target(
            name: "SaliIOS",
            path: ".",
            exclude: ["README.md", "docs"],
            sources: [
                "App",
                "Core",
                "DesignSystem",
                "Models",
                "Features",
            ]
        ),
    ]
)
