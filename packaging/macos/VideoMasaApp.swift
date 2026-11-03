// ─── Video Masa — Native App Shell ───
// Runs launcher.sh, shows a window while first-time setup runs, and gives the app
// normal Dock behaviour: clicking the icon reopens the page, and Quit stops the server.

import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var launcher: Process?
    private var outputBuffer = Data()
    private var appPageURL: URL?
    private var setupFailed = false
    private var window: NSWindow?

    private let headline = NSTextField(labelWithString: "Setting up Video Masa")
    private let detail = NSTextField(wrappingLabelWithString: "Getting started...")
    private let note = NSTextField(
        wrappingLabelWithString: "This only happens once and takes a few minutes. Please stay connected to the internet."
    )
    private let progress = NSProgressIndicator()
    private let buttonRow = NSStackView()

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        if isRunningFromDownload {
            showMoveToApplicationsAlert()
            NSApp.terminate(nil)
            return
        }
        startLauncher()
    }

    // MARK: - Dock behaviour

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openAppPage()
        return true
    }

    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        guard appPageURL != nil else { return nil }
        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "Open Video Masa", action: #selector(openAppPage), keyEquivalent: ""))
        return menu
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let launcher, launcher.isRunning else { return .terminateNow }
        if appPageURL != nil && !isSystemEndingSession && !confirmQuit() {
            return .terminateCancel
        }

        // The launcher stops the server, which deletes its temporary files.
        launcher.terminationHandler = { _ in
            DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) }
        }
        launcher.terminate()
        let launcherID = launcher.processIdentifier
        DispatchQueue.main.asyncAfter(deadline: .now() + 15) {
            if launcher.isRunning { kill(launcherID, SIGKILL) }
        }
        return .terminateLater
    }

    @objc private func openAppPage() {
        if let appPageURL { NSWorkspace.shared.open(appPageURL) }
    }

    // A logout or restart must never wait on a dialog. It marks the quit event with a reason.
    private var isSystemEndingSession: Bool {
        let quitReasonKeyword: AEKeyword = 0x7768_793F  // 'why?'
        return NSAppleEventManager.shared().currentAppleEvent?
            .attributeDescriptor(forKeyword: quitReasonKeyword) != nil
    }

    private func confirmQuit() -> Bool {
        let alert = NSAlert()
        alert.messageText = "Quit Video Masa?"
        alert.informativeText = "Any downloads you haven't saved will be removed."
        alert.addButton(withTitle: "Quit")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        return alert.runModal() == .alertFirstButtonReturn
    }

    // MARK: - Launcher

    private func startLauncher() {
        guard let script = Bundle.main.path(forResource: "launcher", ofType: "sh") else {
            showFailure("Video Masa is missing some of its files. Please download it again.")
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [script]
        process.standardError = FileHandle.nullDevice

        let output = Pipe()
        process.standardOutput = output
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            DispatchQueue.main.async { self?.receive(data) }
        }
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async { self?.launcherFinished() }
        }

        do {
            try process.run()
            launcher = process
        } catch {
            showFailure("Video Masa could not start. Please try again.")
        }
    }

    private func launcherFinished() {
        launcher = nil
        if !setupFailed { NSApp.terminate(nil) }
    }

    private func receive(_ data: Data) {
        outputBuffer.append(data)
        while let newline = outputBuffer.firstIndex(of: 0x0A) {
            let line = String(decoding: outputBuffer[outputBuffer.startIndex..<newline], as: UTF8.self)
            outputBuffer.removeSubrange(outputBuffer.startIndex...newline)
            handle(line)
        }
    }

    private func handle(_ line: String) {
        let parts = line.split(separator: " ", maxSplits: 1).map(String.init)
        let value = parts.count > 1 ? parts[1] : ""
        switch parts.first {
        case "@@STATUS":
            showSetupWindow()
            detail.stringValue = value
        case "@@FAILED":
            showFailure(value)
        case "@@URL":
            appPageURL = URL(string: value)
        case "@@READY":
            window?.orderOut(nil)
        default:
            break
        }
    }

    // MARK: - Setup window

    private func showSetupWindow() {
        if window == nil { buildWindow() }
        guard let window else { return }
        if !window.isVisible {
            window.center()
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    private func buildWindow() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 440, height: 170),
            styleMask: [.titled, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Video Masa"
        window.isReleasedWhenClosed = false

        headline.font = .systemFont(ofSize: 15, weight: .semibold)
        detail.font = .systemFont(ofSize: 13)
        note.font = .systemFont(ofSize: 12)
        note.textColor = .secondaryLabelColor
        progress.style = .bar
        progress.isIndeterminate = true
        progress.startAnimation(nil)
        buttonRow.orientation = .horizontal
        buttonRow.spacing = 8
        buttonRow.isHidden = true

        let icon = NSImageView(image: NSApp.applicationIconImage)
        icon.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            icon.widthAnchor.constraint(equalToConstant: 56),
            icon.heightAnchor.constraint(equalToConstant: 56),
        ])

        let text = NSStackView(views: [headline, detail, progress, note, buttonRow])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 8
        for view in [detail, progress, note] {
            view.translatesAutoresizingMaskIntoConstraints = false
            view.widthAnchor.constraint(equalToConstant: 320).isActive = true
        }

        let content = NSStackView(views: [icon, text])
        content.orientation = .horizontal
        content.alignment = .top
        content.spacing = 16
        content.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)
        window.contentView = content
        self.window = window
    }

    private func showFailure(_ message: String) {
        setupFailed = true
        showSetupWindow()
        headline.stringValue = "Video Masa couldn't finish setting up"
        detail.stringValue = message
        note.isHidden = true
        progress.isHidden = true
        buttonRow.setViews(
            [
                NSButton(title: "Try Again", target: self, action: #selector(tryAgain)),
                NSButton(title: "Show Details", target: self, action: #selector(showDetails)),
                NSButton(title: "Quit", target: NSApp, action: #selector(NSApplication.terminate(_:))),
            ],
            in: .leading
        )
        buttonRow.isHidden = false
    }

    @objc private func tryAgain() {
        setupFailed = false
        headline.stringValue = "Setting up Video Masa"
        detail.stringValue = "Getting started..."
        note.isHidden = false
        progress.isHidden = false
        buttonRow.isHidden = true
        outputBuffer.removeAll()
        startLauncher()
    }

    @objc private func showDetails() {
        let home = ProcessInfo.processInfo.environment["VIDEOMASA_HOME"]
            ?? (NSHomeDirectory() as NSString).appendingPathComponent(".videomasa")
        NSWorkspace.shared.open(URL(fileURLWithPath: home).appendingPathComponent("setup.log"))
    }

    // MARK: - Running from the disk image

    private var isRunningFromDownload: Bool {
        let path = Bundle.main.bundlePath
        return path.hasPrefix("/Volumes/") || path.contains("/AppTranslocation/")
    }

    private func showMoveToApplicationsAlert() {
        let alert = NSAlert()
        alert.messageText = "Move Video Masa to Applications first"
        alert.informativeText = "Drag Video Masa onto the Applications folder in this window, then open it from Applications."
        alert.addButton(withTitle: "OK")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }

    // MARK: - Menu

    private func buildMenu() {
        let appMenu = NSMenu()
        appMenu.addItem(NSMenuItem(title: "Open Video Masa", action: #selector(openAppPage), keyEquivalent: "o"))
        appMenu.addItem(.separator())
        appMenu.addItem(NSMenuItem(title: "Quit Video Masa", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        let appMenuItem = NSMenuItem()
        appMenuItem.submenu = appMenu
        let mainMenu = NSMenu()
        mainMenu.addItem(appMenuItem)
        NSApp.mainMenu = mainMenu
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
