# macOS / Xcode Handoff — Building the Sali iPhone App

The app **source is complete** in `ios/SaliIOS/` but was authored on Kali Linux, where iOS apps cannot be
compiled or signed. This document is everything needed to build, run, and ship it on macOS + Xcode — without
rediscovering the backend (see `API_REFERENCE.md`).

> **Honest status:** nothing in `ios/` has been compiled, run in the Simulator, or signed — there was no
> macOS/Xcode available. The Swift is written to compile against iOS 17 SDK and the Core API is internally
> consistent, but expect to fix minor compile issues on first build (the normal cost of writing iOS code
> off-device). The **backend** it targets is fully implemented and tested (`tests/test_api_*.py`).

## 1. What's in the box

```
ios/SaliIOS/
  Package.swift              # SPM library target (logic layers) — no app/executable target
  App/                       # SaliApp (@main), AppState (composition root), RootView, MainTab
  Core/
    Config/                  # APIEnvironment, APIConfiguration (dev/staging/prod, WS URL derivation)
    Auth/                    # Keychain, TokenStore, AuthService (enroll/refresh/sign-out)
    Networking/              # APIClient (actor, auto-refresh on 401), APIError
    WebSocket/               # WebSocketClient (auth, subscribe after_seq, reconnect+backoff)
    Realtime/                # SaliEvent (event model + decoding), ConnectionState
  DesignSystem/              # Theme, Components (Loadable, states, pills, badges)
  Models/                    # DomainModels (all API response types)
  Features/                  # Onboarding, Chat, Activity, Tasks, Memory, Learning, Life, System,
                             #   Notifications, Settings
  docs/                      # API_REFERENCE.md, CLOUDFLARE.md, XCODE_HANDOFF.md
```

Architecture: SwiftUI + async/await, MVVM. Layers are decoupled (§3) — screens depend on `AppState` /
`APIClient` / models, never on raw HTTP. No third-party dependencies; Markdown is rendered in-app; charts use
Apple's `Charts` framework.

## 2. Create the Xcode App target

The package intentionally has no app target (SPM can't build an iOS app bundle). Two options:

**Option A — new App project embedding the package (recommended):**
1. Xcode → New → Project → iOS → App. Product name `Sali`, Interface SwiftUI, Language Swift, min iOS 17.
2. Delete the generated `ContentView.swift` and the generated `@main App` struct (the package already
   provides `SaliApp` as `@main`), OR keep the generated `@main` and instead present `RootView()` from it
   (simplest: keep the package's `SaliApp` as `@main` and remove the template's).
3. File → Add Package Dependencies → Add Local… → select `ios/SaliIOS`. Add the `SaliIOS` library to the
   app target's Frameworks.
4. Because `SaliApp` is `@main` inside the library, either (a) mark the library types you need `public`
   (already done for Core/AppState/DesignSystem/models) and re-declare a thin `@main` in the app target that
   shows `RootView().environmentObject(AppState())…`, or (b) move `App/` sources into the app target
   directly. **Simplest reliable path:** drag the `App/`, `Core/`, `DesignSystem/`, `Models/`, `Features/`
   folders straight into the app target (Option B) and skip the package.

**Option B — drag the folders into a fresh App target (fastest to a running build):**
1. New iOS App project (SwiftUI, iOS 17).
2. Remove the template `ContentView.swift` and template `App` struct.
3. Drag `App Core DesignSystem Models Features` into the project ("Copy items if needed", add to the app
   target). The package's `SaliApp` becomes the entry point.
4. Build. Fix any access-control mismatches (in a single target, the `public` markers are harmless).

## 3. Signing & capabilities

- Set your **Development Team** (Signing & Capabilities). A free Apple ID works for on-device debug builds;
  a paid Apple Developer account is required for TestFlight/App Store and for **push**.
- Bundle identifier: e.g. `com.salieno.sali`.
- **Keychain** works out of the box (no entitlement needed for generic passwords in your own app).
- **App Transport Security:** production uses HTTPS (`sali.salieno.com`) — no ATS exception needed. For LAN
  development over `http://<ip>:8080`, add a temporary ATS exception (Info.plist
  `NSAppTransportSecurity` → `NSAllowsLocalNetworking = YES`) or use the tunnel URL even in dev.
- **Background modes:** enable *Background fetch* / *Remote notifications* only when wiring push (below).

## 4. Push notifications (deferred — needs the Apple Developer account, §11)

The app ships a clean push *interface* (`Features/Notifications/PushManager.swift`) but does NOT fake push.
To enable it later on macOS:
1. Add the **Push Notifications** capability (creates the `aps-environment` entitlement).
2. In the App Store Connect / Developer portal, create an **APNs Auth Key (.p8)** and note the Key ID + Team
   ID. The **server** side (a small APNs sender) is NOT yet implemented — build it to send to the device's
   registered token.
3. In `AppDelegate` (or `UIApplicationDelegateAdaptor`): implement
   `didRegisterForRemoteNotificationsWithDeviceToken`, convert the `Data` token to hex, and call
   `PushManager.submitToken(_:deviceId:environment:api:)` → `POST /api/v1/devices/{id}/push-token`.
4. Request authorization via `UNUserNotificationCenter` on a sensible prompt moment.
The backend already stores push tokens per device (`api_device.push_token` / `push_environment`); what
remains is the APNs sending service (documented, not built — no Apple account was available).

## 5. Configure the endpoint

No secrets are embedded. On first launch the app shows **Onboarding**:
1. On the Kali host, run `sali serve` (dev: `--host 0.0.0.0`; prod behind the tunnel: `--host 127.0.0.1`).
2. On the host, run `sali enroll-code` → it prints a one-time code (`XXXXX-XXXXX`).
3. In the app: Onboarding → set the server address if needed (Advanced → environment / base URL) → enter the
   code + a device name → **Pair this iPhone**. Tokens are stored in the Keychain; the device is authorized.
4. Change environments anytime in **Settings → Connection** (no recompile, §32). Production defaults to
   `https://sali.salieno.com`.

## 6. Running against the backend

- Dev, same LAN: `sali serve --host 0.0.0.0 --port 8080`; app Development env → `http://<kali-ip>:8080`.
- Prod: `cloudflared` tunnel up (see `CLOUDFLARE.md`); app Production env → `https://sali.salieno.com`.
- Verify reachability: Settings → Test connection (hits `/healthz`).

## 7. Tests on macOS

The logic layers are unit-testable without a device: add an XCTest target and test `TokenStore`
(Keychain round-trip on the sim), `APIConfiguration.webSocketURL` derivation, `SaliEvent.decode`, the
`MarkdownText` block parser, and `ChatViewModel.ingest` state machine with synthetic events. The backend
contract itself is already covered by `tests/test_api_*.py` on the Sali host.

## 8. Known follow-ups (be honest with the next dev)

- First compile will likely need small fixes (this code was never run through swiftc).
- APNs **sending** service is not built (needs the Apple account).
- The `Charts`-based history in System is fed by an in-memory rolling buffer (resets on relaunch) — wire it
  to `/resource-incidents` + periodic `/system` samples for longer history if desired.
- Consider adding certificate pinning or Cloudflare Access service-token headers for extra hardening.
