import SwiftUI
import AppKit

/// Whether closing the window keeps Rocky in the menu bar to deliver alerts.
let keepInMenuBarKey = "keepRunningInMenuBar"

final class AppDelegate: NSObject, NSApplicationDelegate {
    var runtime: RuntimeManager?
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        !(UserDefaults.standard.object(forKey: keepInMenuBarKey) as? Bool ?? true)
    }
    func applicationWillTerminate(_ notification: Notification) {
        // The background service keeps running; only stop observing it.
        MainActor.assumeIsolated { runtime?.disconnect() }
    }
}

@main
struct RockyDesktopApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = AppModel(delivery: SystemNotifier.makeIfAvailable())
    @AppStorage(keepInMenuBarKey) private var keepInMenuBar = true

    var body: some Scene {
        WindowGroup(id: "main") {
            MainWindow(model: model) { delegate.runtime = model.runtime }
        }
        .defaultSize(width: 1180, height: 800)
        .windowToolbarStyle(.unified)
        .commands {
            SidebarCommands()
            CommandGroup(replacing: .newItem) {
                Button("新建任务") { model.newTask() }.keyboardShortcut("n")
            }
            CommandGroup(replacing: .appSettings) {
                Button("设置…") { model.showSettings = true }.keyboardShortcut(",")
            }
            CommandMenu("任务") {
                Button("显示任务详情") { model.showInspector.toggle() }.keyboardShortcut("i", modifiers: [.command, .option])
                Button("刷新") { Task { await model.refresh() } }.keyboardShortcut("r")
            }
        }
        MenuBarExtra(isInserted: $keepInMenuBar) {
            MenuBarContent(model: model)
        } label: {
            Image(systemName: model.attentionGoals.isEmpty ? "circle.hexagongrid" : "hand.raised.fill")
        }
    }
}

private struct MainWindow: View {
    @ObservedObject var model: AppModel
    let onStart: () -> Void
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        WorkspaceView(model: model)
            .frame(minWidth: 900, minHeight: 650)
            .onAppear {
                onStart()
                model.openMainWindow = { openWindow(id: "main") }
                model.startIfNeeded()
            }
    }
}

/// Shown while the window is closed: what waits for the owner, and a way back.
private struct MenuBarContent: View {
    @ObservedObject var model: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        let waiting = model.attentionGoals
        if waiting.isEmpty {
            Text(model.goals.contains { !$0.state.isTerminal } ? "任务正在后台运行" : "没有进行中的任务")
        } else {
            Text("\(waiting.count) 个任务在等你处理")
            ForEach(waiting.prefix(8)) { goal in
                Button((goal.state.pending_approval != nil ? "批准：" : "回复：") + goal.title) { open(goal.id) }
            }
        }
        Divider()
        Button("打开 Rocky") { open(nil) }
        Button("退出 Rocky") { NSApp.terminate(nil) }.keyboardShortcut("q")
    }

    private func open(_ id: String?) {
        model.openMainWindow = model.openMainWindow ?? { openWindow(id: "main") }
        if let id { model.reveal(id) } else {
            if let window = NSApp.mainWorkspaceWindow {
                window.makeKeyAndOrderFront(nil)
            } else {
                openWindow(id: "main")
            }
            NSApp.activate(ignoringOtherApps: true)
        }
    }
}
