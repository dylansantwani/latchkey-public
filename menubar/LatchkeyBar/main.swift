// LatchkeyBar — latchkey's viewer, as a small toggle in the menu bar.
//
// The viewer's web UI is the human's window into what an agent is doing: the latest
// frame of each session, the agent's pointer between frames, every action as it lands.
// This puts that page one click away in the menu bar, and takes it away again:
//
//   left click    the panel: the web UI of whichever latchkey server has the sessions
//   esc           put it away
//   right click   the menu: status, open in browser, copy the address, sessions, a
//                 viewer of its own for a scratch script, launch at login, quit
//
// Which server: every latchkey process has its own viewer port, and a port that answers
// with no sessions looks connected and empty at the same time. So the same policy the
// Electron viewer uses applies here - the ports in ~/.latchkey/viewers.json, dead pids
// ignored, then 8788-8798 - preferring the server that actually has sessions, because
// that is the one an agent is driving.
//
// QA, without needing a menu bar to look at:
//   ./LatchkeyBar --status                   one line of state (also /tmp/latchkeybar.status)
//   ./LatchkeyBar --renderlabel out.png      the menu-bar glyph, both states
//   ./LatchkeyBar --render out.png           the panel: the live page, or the empty state
import AppKit
import WebKit

let STATUS_PATH = "/tmp/latchkeybar.status"
let LOCK_PATH = "/tmp/latchkeybar.pid"
let REGISTRY_PATH = NSString(string: "~/.latchkey/viewers.json").expandingTildeInPath
let SCAN_PORTS = Array(8788...8798)
let LOGIN_LABEL = "com.dylan.latchkeybar"
let PANEL_SIZE = NSSize(width: 940, height: 660)

// Where latchkey lives, and which python runs it. Both overridable, because a window
// server app is started by launchd, not by a shell that already has either.
let LATCHKEY_ROOT = ProcessInfo.processInfo.environment["LATCHKEY_ROOT"]
    ?? ("\(NSHomeDirectory())/tools/latchkey")
let LATCHKEY_PYTHON = ProcessInfo.processInfo.environment["LATCHKEY_PYTHON"]
    ?? "/usr/bin/python3"


// -- finding the server the agent is using -----------------------------------

struct Viewer {
    let port: Int
    let sessions: [String]
    let viewers: Int
    let uptime: Double
    let source: String        // "registry" or "scan"
    var url: URL { URL(string: "http://127.0.0.1:\(port)/")! }
    var address: String { "127.0.0.1:\(port)" }
    var report: String {
        let count = sessions.isEmpty
            ? "no sessions"
            : "\(sessions.count) session\(sessions.count == 1 ? "" : "s")"
        return "live on \(address) · \(count)"
    }
}

/// The ports running viewers wrote down, minus the ones whose writer has died.
func registryPorts() -> [Int] {
    guard let data = FileManager.default.contents(atPath: REGISTRY_PATH),
          let rows = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]]
    else { return [] }
    return rows.compactMap { row in
        guard let port = row["port"] as? Int else { return nil }
        guard let pid = row["pid"] as? Int else { return port }
        return kill(pid_t(pid), 0) == 0 ? port : nil
    }
}

/// `GET /health` on one port, or nil if nothing is listening (or it answers oddly).
func health(_ port: Int, timeout: TimeInterval = 0.4) -> [String: Any]? {
    var request = URLRequest(url: URL(string: "http://127.0.0.1:\(port)/health")!)
    request.timeoutInterval = timeout
    request.cachePolicy = .reloadIgnoringLocalCacheData
    let done = DispatchSemaphore(value: 0)
    var payload: [String: Any]?
    URLSession.shared.dataTask(with: request) { data, _, _ in
        if let data,
           let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           (object["ok"] as? Bool) == true {
            payload = object
        }
        done.signal()
    }.resume()
    _ = done.wait(timeout: .now() + timeout + 0.3)
    return payload
}

/// Every live latchkey viewer, best first: the one with sessions is the agent's.
func liveViewers() -> [Viewer] {
    let known = registryPorts()
    var seen = Set<Int>()
    let candidates = (known + SCAN_PORTS).filter { seen.insert($0).inserted }
    var found: [Viewer] = []
    let lock = NSLock()
    DispatchQueue.concurrentPerform(iterations: candidates.count) { index in
        let port = candidates[index]
        guard let report = health(port) else { return }
        let viewer = Viewer(port: port,
                            sessions: (report["sessions"] as? [String]) ?? [],
                            viewers: (report["viewers"] as? Int) ?? 0,
                            uptime: (report["uptime_s"] as? Double) ?? 0,
                            source: known.contains(port) ? "registry" : "scan")
        lock.lock()
        found.append(viewer)
        lock.unlock()
    }
    return found.sorted { left, right in
        if left.sessions.count != right.sessions.count {
            return left.sessions.count > right.sessions.count
        }
        if (left.source == "registry") != (right.source == "registry") {
            return left.source == "registry"
        }
        return left.port < right.port
    }
}

/// The viewer to show: a pinned port wins when it has sessions, and is only passed over
/// when it has none and another live server has some - the rule the viewer app uses too.
func pickViewer(pinned: Int?) -> Viewer? {
    let live = liveViewers()
    if let pinned, let match = live.first(where: { $0.port == pinned }),
       !match.sessions.isEmpty || !live.contains(where: { !$0.sessions.isEmpty }) {
        return match
    }
    return live.first
}


// -- the empty state ---------------------------------------------------------

/// What the panel says when nothing is listening. Returned with the field it wraps so
/// the app can put the reason a viewer of its own did not come up into it.
func makeEmptyStateView(target: AnyObject? = nil, start: Selector? = nil, again: Selector? = nil)
    -> (view: NSView, detail: NSTextField) {
    let view = NSView(frame: NSRect(origin: .zero, size: PANEL_SIZE))
    view.wantsLayer = true
    view.layer?.backgroundColor = NSColor(calibratedWhite: 0.08, alpha: 1).cgColor

    let title = NSTextField(labelWithString: "No latchkey viewer is running")
    title.font = .systemFont(ofSize: 15, weight: .semibold)
    title.textColor = .white

    let detail = NSTextField(wrappingLabelWithString: "")
    detail.font = .systemFont(ofSize: 12)
    detail.textColor = NSColor(calibratedWhite: 0.72, alpha: 1)
    detail.alignment = .center
    detail.maximumNumberOfLines = 0
    detail.preferredMaxLayoutWidth = 420

    let buttons = NSStackView(views: [
        NSButton(title: "Start a viewer here", target: target, action: start),
        NSButton(title: "Look again", target: target, action: again),
    ])
    buttons.orientation = .horizontal
    buttons.spacing = 8
    for case let button as NSButton in buttons.views { button.bezelStyle = .rounded }

    let stack = NSStackView(views: [title, detail, buttons])
    stack.orientation = .vertical
    stack.alignment = .centerX
    stack.spacing = 14
    stack.translatesAutoresizingMaskIntoConstraints = false
    view.addSubview(stack)
    NSLayoutConstraint.activate([
        stack.centerXAnchor.constraint(equalTo: view.centerXAnchor),
        stack.centerYAnchor.constraint(equalTo: view.centerYAnchor),
        detail.widthAnchor.constraint(equalToConstant: 440),
    ])
    return (view, detail)
}

func emptyStateText(note: String?) -> String {
    var text = """
        A viewer is the window into what an agent is doing. It comes up with the agent's \
        latchkey server, on whichever port that process was given \
        (LATCHKEY_VIEWER_PORT, usually 8788).

        LatchkeyBar looks at \(SCAN_PORTS.first!)-\(SCAN_PORTS.last!) every two seconds, \
        and nothing is listening there yet.
        """
    if let note {
        text += "\n\n\(note)"
    }
    return text
}


// -- the app -----------------------------------------------------------------

final class Panel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var item: NSStatusItem!
    private var timer: Timer?
    private var panel: Panel?
    private var web: WKWebView?
    private var empty: NSView?
    private var emptyDetail: NSTextField?
    private var viewer: Viewer?
    private var loadedPort: Int?
    private var pinned: Int?
    /// A viewer this app started itself, for the case where no agent is running.
    private var child: Process?
    private var childNote: String?

    init(pinned: Int?) {
        self.pinned = pinned
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.target = self
        item.button?.action = #selector(clicked(_:))
        // One control, both buttons: left toggles the panel, right opens the menu.
        item.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])

        // Escape puts the panel away wherever the keyboard focus is inside it.
        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            if event.keyCode == 53, let panel = self?.panel, panel.isVisible {
                panel.orderOut(nil)
                return nil
            }
            return event
        }

        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.refresh()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        child?.terminate()
        try? FileManager.default.removeItem(atPath: LOCK_PATH)
    }

    // -- state ---------------------------------------------------------------

    @objc private func refresh() {
        viewer = pickViewer(pinned: pinned)
        updateIcon()
        writeStatus()
        if let panel, panel.isVisible {
            show(viewer: viewer, force: false)
        }
    }

    private func updateIcon() {
        guard let button = item.button else { return }
        let live = viewer != nil
        button.image = symbol(live ? "rectangle.inset.filled" : "rectangle")
        // Dimmed when there is nothing to watch, so the icon reads as a state.
        button.appearsDisabled = !live
        button.toolTip = viewer.map { "latchkey viewer — \($0.report)" }
            ?? "latchkey viewer — nothing listening (a viewer comes up with the agent)"
    }

    private func symbol(_ name: String) -> NSImage {
        let image = NSImage(systemSymbolName: name, accessibilityDescription: "latchkey viewer")
            ?? NSImage(size: NSSize(width: 16, height: 16))
        image.isTemplate = true
        image.size = NSSize(width: 16, height: 16)
        return image
    }

    /// The one line the app writes every refresh, and `--status` prints. It is what to
    /// read when the icon looks wrong: which port, how many sessions, and what it found.
    func statusLine() -> String {
        var parts = ["latchkeybar"]
        if let viewer {
            parts.append("live=\(viewer.port)")
            parts.append("sessions=\(viewer.sessions.count)")
            parts.append("viewers=\(viewer.viewers)")
            parts.append("source=\(viewer.source)")
        } else {
            parts.append("live=none")
        }
        parts.append("registry=\(registryPorts().count)")
        // Only the running app has one, and a status item that failed to take would
        // otherwise be silent: the icon would simply never appear.
        if let item { parts.append("button=\(item.button == nil ? "missing" : "ok")") }
        if let child, child.isRunning { parts.append("own_child=\(child.processIdentifier)") }
        if let childNote { parts.append("note=\(childNote)") }
        if let pinned { parts.append("pinned=\(pinned)") }
        return parts.joined(separator: " ")
    }

    private func writeStatus() {
        try? (statusLine() + "\n").write(toFile: STATUS_PATH, atomically: true, encoding: .utf8)
    }

    /// The line as of now, for `--status`: it probes instead of trusting the last tick.
    func statusNow() -> String {
        viewer = pickViewer(pinned: pinned)
        return statusLine()
    }

    // -- clicks --------------------------------------------------------------

    @objc private func clicked(_ sender: Any?) {
        let event = NSApp.currentEvent
        let rightClick = event?.type == .rightMouseUp
            || event?.modifierFlags.contains(.control) == true
        if rightClick {
            showMenu()
        } else {
            togglePanel()
        }
    }

    private func togglePanel() {
        if let panel, panel.isVisible {
            panel.orderOut(nil)
            return
        }
        let panel = ensurePanel()
        show(viewer: viewer, force: false)
        position(panel)
        panel.makeKeyAndOrderFront(nil)
    }

    /// Point the web view at the chosen viewer. Only a *different* port reloads the page,
    /// so the two-second refresh never interrupts what the page is showing.
    private func show(viewer: Viewer?, force: Bool) {
        let panel = ensurePanel()
        if let viewer {
            web?.isHidden = false
            empty?.isHidden = true
            if let child, child.isRunning { childNote = nil }
            if force || loadedPort != viewer.port {
                web?.load(URLRequest(url: viewer.url))
                loadedPort = viewer.port
            }
            let sessions = viewer.sessions.isEmpty
                ? "no sessions"
                : viewer.sessions.joined(separator: ", ")
            panel.title = "latchkey viewer · \(viewer.address) · \(sessions)"
        } else {
            web?.isHidden = true
            empty?.isHidden = false
            loadedPort = nil
            panel.title = "latchkey viewer"
            emptyDetail?.stringValue = emptyStateText(note: childNote)
        }
    }

    // -- the panel -----------------------------------------------------------

    private func ensurePanel() -> Panel {
        if let panel { return panel }
        let panel = Panel(contentRect: NSRect(origin: .zero, size: PANEL_SIZE),
                          styleMask: [.titled, .closable, .resizable, .utilityWindow,
                                      .nonactivatingPanel],
                          backing: .buffered, defer: false)
        panel.title = "latchkey viewer"
        panel.titlebarAppearsTransparent = true
        panel.isMovableByWindowBackground = true
        panel.level = .floating
        panel.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        panel.appearance = NSAppearance(named: .darkAqua)
        panel.backgroundColor = NSColor(calibratedWhite: 0.08, alpha: 1.0)
        panel.minSize = NSSize(width: 520, height: 360)
        panel.isReleasedWhenClosed = false

        let web = WKWebView(frame: panel.contentView?.bounds ?? .zero)
        web.autoresizingMask = [.width, .height]
        panel.contentView?.addSubview(web)
        self.web = web

        let (empty, detail) = makeEmptyStateView(target: self,
                                                 start: #selector(startOwnViewer),
                                                 again: #selector(lookAgain))
        empty.frame = panel.contentView?.bounds ?? .zero
        empty.autoresizingMask = [.width, .height]
        empty.isHidden = true
        panel.contentView?.addSubview(empty)
        self.empty = empty
        self.emptyDetail = detail

        self.panel = panel
        return panel
    }

    private func position(_ panel: Panel) {
        guard let button = item.button, let bar = button.window,
              let screen = bar.screen ?? NSScreen.main else { return }
        var origin = NSPoint(x: bar.frame.midX - panel.frame.width / 2,
                             y: bar.frame.minY - panel.frame.height - 6)
        let visible = screen.visibleFrame
        origin.x = min(max(origin.x, visible.minX + 8), visible.maxX - panel.frame.width - 8)
        origin.y = max(origin.y, visible.minY + 8)
        panel.setFrameOrigin(origin)
    }

    @objc private func lookAgain() {
        refresh()
        show(viewer: viewer, force: true)
    }

    // -- a viewer of our own, for when no agent is running --------------------

    @objc private func startOwnViewer() {
        guard child == nil else { return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: LATCHKEY_PYTHON)
        process.arguments = ["-m", "latchkey", "watch", "--port", "8788"]
        process.currentDirectoryURL = URL(fileURLWithPath: LATCHKEY_ROOT)
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = LATCHKEY_ROOT
        process.environment = environment

        let errors = Pipe()
        process.standardError = errors
        process.terminationHandler = { [weak self] finished in
            let text = String(data: errors.fileHandleForReading.readDataToEndOfFile(),
                              encoding: .utf8) ?? ""
            DispatchQueue.main.async {
                guard let self else { return }
                self.child = nil
                if finished.terminationStatus != 0 {
                    self.childNote = "The viewer LatchkeyBar started stopped: "
                        + (text.split(separator: "\n").last.map(String.init)
                           ?? "exit \(finished.terminationStatus)")
                }
                self.refresh()
            }
        }
        do {
            try process.run()
            child = process
            childNote = nil
        } catch {
            childNote = "Could not start \(LATCHKEY_PYTHON): \(error.localizedDescription)"
        }
        refresh()
    }

    @objc private func stopOwnViewer() {
        child?.terminate()
        child = nil
        refresh()
    }

    @objc private func copyAddress() {
        guard let viewer else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString("http://\(viewer.address)/", forType: .string)
    }

    @objc private func openInBrowser() {
        guard let viewer else { return }
        NSWorkspace.shared.open(viewer.url)
    }

    @objc private func refreshNow() {
        refresh()
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    // -- the menu ------------------------------------------------------------

    private func showMenu() {
        guard let button = item.button else { return }
        let menu = NSMenu()
        menu.autoenablesItems = false

        menu.addItem(disabled(viewer.map { "latchkey viewer — \($0.report)" }
            ?? "latchkey viewer — nothing listening"))
        if let viewer {
            if !viewer.sessions.isEmpty {
                menu.addItem(disabled("Sessions: \(viewer.sessions.joined(separator: ", "))"))
            }
            if viewer.uptime > 0 {
                menu.addItem(disabled("Up \(duration(viewer.uptime))"))
            }
            menu.addItem(.separator())
            menu.addItem(entry("Open in browser", #selector(openInBrowser)))
            menu.addItem(entry("Copy address", #selector(copyAddress)))
        } else {
            menu.addItem(.separator())
            menu.addItem(entry("Start a viewer here", #selector(startOwnViewer),
                               enabled: child == nil))
        }
        if child != nil {
            menu.addItem(entry("Stop the viewer LatchkeyBar started", #selector(stopOwnViewer)))
        }
        if let childNote {
            menu.addItem(disabled(childNote))
        }

        menu.addItem(.separator())
        menu.addItem(entry("Look again now", #selector(refreshNow)))
        let login = entry("Launch at login", #selector(toggleLogin))
        login.state = Login.isInstalled ? .on : .off
        menu.addItem(login)
        menu.addItem(.separator())
        menu.addItem(entry("Quit LatchkeyBar", #selector(quit)))

        menu.popUp(positioning: nil,
                   at: NSPoint(x: 0, y: button.bounds.height + 6), in: button)
    }

    private func entry(_ title: String, _ action: Selector, enabled: Bool = true) -> NSMenuItem {
        let menuItem = NSMenuItem(title: title, action: action, keyEquivalent: "")
        menuItem.target = self
        menuItem.isEnabled = enabled
        return menuItem
    }

    private func disabled(_ title: String) -> NSMenuItem {
        let menuItem = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        menuItem.isEnabled = false
        return menuItem
    }

    private func duration(_ seconds: Double) -> String {
        let total = Int(seconds)
        if total < 3600 { return "\(total / 60)m" }
        return "\(total / 3600)h \((total % 3600) / 60)m"
    }

    @objc private func toggleLogin() {
        Login.set(!Login.isInstalled)
        refresh()
    }
}


// -- launch at login ---------------------------------------------------------

enum Login {
    static var path: String {
        NSString(string: "~/Library/LaunchAgents/\(LOGIN_LABEL).plist").expandingTildeInPath
    }

    static var isInstalled: Bool { FileManager.default.fileExists(atPath: path) }

    static var binary: String { Bundle.main.executablePath ?? CommandLine.arguments[0] }

    /// True when this process is the one the login agent started.
    static var isAgentProcess: Bool {
        ProcessInfo.processInfo.environment["XPC_SERVICE_NAME"] == LOGIN_LABEL
    }

    static var isLoaded: Bool {
        run("/bin/launchctl", ["print", "gui/\(getuid())/\(LOGIN_LABEL)"], quiet: true) == 0
    }

    /// Idempotent, and it never restarts the copy the user is looking at: turning it on
    /// writes the plist and loads the job only if it is not loaded already, and turning it
    /// off removes the plist and unloads only a job some *other* copy is running under.
    static func set(_ wanted: Bool) {
        if wanted {
            write()
            if !isLoaded {
                run("/bin/launchctl", ["bootstrap", "gui/\(getuid())", path])
            }
        } else {
            try? FileManager.default.removeItem(atPath: path)
            if isLoaded, !isAgentProcess {
                run("/bin/launchctl", ["bootout", "gui/\(getuid())", path], quiet: true)
            }
        }
    }

    private static func write() {
        let plist = """
            <?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
            "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
            <plist version="1.0">
            <dict>
                <key>Label</key><string>\(LOGIN_LABEL)</string>
                <key>ProgramArguments</key>
                <array><string>\(binary)</string></array>
                <key>RunAtLoad</key><true/>
                <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
                <key>ProcessType</key><string>Interactive</string>
                <key>LimitLoadToSessionType</key><string>Aqua</string>
                <key>StandardOutPath</key><string>/tmp/latchkeybar.out.log</string>
                <key>StandardErrorPath</key><string>/tmp/latchkeybar.err.log</string>
            </dict>
            </plist>
            """
        FileManager.default.createFile(atPath: path, contents: plist.data(using: .utf8))
    }

    @discardableResult
    private static func run(_ tool: String, _ arguments: [String],
                            quiet: Bool = false) -> Int32 {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: tool)
        process.arguments = arguments
        if quiet {
            process.standardError = FileHandle.nullDevice
            process.standardOutput = FileHandle.nullDevice
        }
        do {
            try process.run()
        } catch {
            return -1
        }
        process.waitUntilExit()
        return process.terminationStatus
    }
}


// -- one instance ------------------------------------------------------------

/// A second copy would be a second icon, so a run in the GUI takes a pid file first.
/// A stale file (a crash, a kill -9) is not believed: the pid in it has to be alive.
func alreadyRunning() -> Bool {
    guard let text = try? String(contentsOfFile: LOCK_PATH, encoding: .utf8),
          let pid = pid_t(text.trimmingCharacters(in: .whitespacesAndNewlines)) else {
        return false
    }
    return pid != getpid() && kill(pid, 0) == 0
}

func takeLock() {
    try? "\(getpid())\n".write(toFile: LOCK_PATH, atomically: true, encoding: .utf8)
}


// -- the QA modes ------------------------------------------------------------

/// A menu bar strip with both states of the glyph, so the icon can be looked at without
/// a menu bar and without screen-recording permission.
func renderLabel(to path: String) -> Int32 {
    let width: CGFloat = 320, height: CGFloat = 78
    let image = NSImage(size: NSSize(width: width, height: height))
    image.lockFocus()
    NSColor(calibratedWhite: 0.93, alpha: 1).setFill()
    NSRect(x: 0, y: 0, width: width, height: height).fill()
    NSColor(calibratedWhite: 0.78, alpha: 1).setFill()
    NSRect(x: 0, y: 0, width: width, height: 1).fill()

    func draw(_ live: Bool, at x: CGFloat, caption: String) {
        let symbol = NSImage(systemSymbolName: live ? "rectangle.inset.filled" : "rectangle",
                             accessibilityDescription: nil) ?? NSImage()
        symbol.isTemplate = true
        let scale: CGFloat = 2.6
        let size = NSSize(width: 16 * scale, height: 16 * scale)
        (live ? NSColor.black : NSColor(calibratedWhite: 0.6, alpha: 1)).set()
        symbol.draw(in: NSRect(x: x - size.width / 2, y: height / 2 - 8,
                               width: size.width, height: size.height),
                    from: .zero, operation: .sourceOver, fraction: 1)
        let label = NSAttributedString(string: caption, attributes: [
            .font: NSFont.systemFont(ofSize: 11),
            .foregroundColor: NSColor(calibratedWhite: 0.15, alpha: 1),
        ])
        let size2 = label.size()
        label.draw(at: NSPoint(x: x - size2.width / 2, y: 7))
    }

    draw(false, at: 85, caption: "nothing listening (dimmed)")
    draw(true, at: 235, caption: "viewer live")
    image.unlockFocus()

    guard let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
          let data = rep.representation(using: .png, properties: [:]) else {
        FileHandle.standardError.write("could not draw the label\n".data(using: .utf8)!)
        return 1
    }
    do {
        try data.write(to: URL(fileURLWithPath: path))
        print("wrote \(path)")
        return 0
    } catch {
        FileHandle.standardError.write("\(error)\n".data(using: .utf8)!)
        return 1
    }
}

final class PageLoad: NSObject, WKNavigationDelegate {
    var finished = false
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { finished = true }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        finished = true
    }
}

/// The panel as a PNG: the real web UI when a viewer is live, otherwise the empty state.
func renderPanel(to path: String, pinned: Int?) -> Int32 {
    NSApplication.shared.setActivationPolicy(.accessory)
    let container = NSWindow(contentRect: NSRect(origin: .zero, size: PANEL_SIZE),
                             styleMask: [.titled], backing: .buffered, defer: false)
    container.appearance = NSAppearance(named: .darkAqua)
    container.backgroundColor = NSColor(calibratedWhite: 0.08, alpha: 1)

    func save(_ image: NSImage, _ what: String) -> Int32 {
        guard let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
              let data = rep.representation(using: .png, properties: [:]),
              (try? data.write(to: URL(fileURLWithPath: path))) != nil else {
            FileHandle.standardError.write("could not write \(path)\n".data(using: .utf8)!)
            return 1
        }
        print("wrote \(path) (\(what))")
        return 0
    }

    func pump(until condition: () -> Bool, timeout: TimeInterval) {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition(), Date() < deadline {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
        }
    }

    guard let viewer = pickViewer(pinned: pinned) else {
        let (view, detail) = makeEmptyStateView()
        detail.stringValue = emptyStateText(note: nil)
        container.contentView = view
        container.orderBack(nil)
        // One turn of the run loop, so the stack view lays out and the layer draws.
        RunLoop.current.run(until: Date().addingTimeInterval(0.4))
        guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return 1 }
        view.cacheDisplay(in: view.bounds, to: rep)
        guard let data = rep.representation(using: .png, properties: [:]),
              (try? data.write(to: URL(fileURLWithPath: path))) != nil else { return 1 }
        print("wrote \(path) (no viewer live)")
        return 0
    }

    let load = PageLoad()
    let web = WKWebView(frame: NSRect(origin: .zero, size: PANEL_SIZE))
    web.navigationDelegate = load
    container.contentView = web
    container.orderBack(nil)
    web.load(URLRequest(url: viewer.url))
    pump(until: { load.finished }, timeout: 15)
    // Then let the page's own refresh cycle run: it draws a frame only after its first
    // poll, so a snapshot taken the moment the document loads shows the empty state.
    RunLoop.current.run(until: Date().addingTimeInterval(3.0))

    var shot: NSImage?
    web.takeSnapshot(with: nil) { image, _ in shot = image }
    pump(until: { shot != nil }, timeout: 10)
    guard let shot else {
        FileHandle.standardError.write("the page did not paint in time\n".data(using: .utf8)!)
        return 1
    }
    return save(shot, "\(viewer.address), \(viewer.sessions.count) session(s)")
}


// -- entry point -------------------------------------------------------------

func pinnedPort(from arguments: [String]) -> Int? {
    if let index = arguments.firstIndex(of: "--port"), index + 1 < arguments.count {
        return Int(arguments[index + 1])
    }
    if let value = ProcessInfo.processInfo.environment["LATCHKEY_VIEWER_PORT"],
       let port = Int(value) {
        return port
    }
    return nil
}

let arguments = CommandLine.arguments
let pinned = pinnedPort(from: arguments)

if arguments.contains("--status") {
    let line = AppDelegate(pinned: pinned).statusNow()
    print(line)
    try? (line + "\n").write(toFile: STATUS_PATH, atomically: true, encoding: .utf8)
    exit(0)
}

if let index = arguments.firstIndex(of: "--help"), index > 0 {
    print("""
        LatchkeyBar — the latchkey viewer as a menu bar toggle.

          (no arguments)            take the menu bar (left click: the web UI panel,
                                    right click: the menu, esc: put it away)
          --status                  one line of what it can see, and write it down
          --render out.png          the panel as a PNG (live page, or the empty state)
          --renderlabel out.png     the menu-bar glyph, both states
          --login on|off            install or remove the login agent
          --port N                  pin a viewer port instead of picking the busiest
        """)
    exit(0)
}

if let index = arguments.firstIndex(of: "--renderlabel"), index + 1 < arguments.count {
    exit(renderLabel(to: arguments[index + 1]))
}

// The one thing the installer cannot do for itself: write the login agent. It is the
// same plist the menu's "Launch at login" writes, from the same place.
if let index = arguments.firstIndex(of: "--login"), index + 1 < arguments.count {
    Login.set(arguments[index + 1] != "off")
    print("launch at login: \(Login.isInstalled ? "on" : "off") (\(Login.path))")
    exit(0)
}

if let index = arguments.firstIndex(of: "--render"), index + 1 < arguments.count {
    exit(renderPanel(to: arguments[index + 1], pinned: pinned))
}

if alreadyRunning() {
    FileHandle.standardError.write("latchkeybar is already in the menu bar\n".data(using: .utf8)!)
    exit(0)
}
takeLock()

let application = NSApplication.shared
let delegate = AppDelegate(pinned: pinned)
application.delegate = delegate
application.setActivationPolicy(.accessory)
application.run()
