import SwiftUI
import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    var runtime: RuntimeManager?
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) {
        // The background service keeps running; only stop observing it.
        MainActor.assumeIsolated { runtime?.disconnect() }
    }
}

@main
struct RockyDesktopApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            WorkspaceView(model: model)
                .frame(minWidth: 900, minHeight: 650)
                .task {
                    delegate.runtime = model.runtime
                    await model.run()
                }
        }
        .defaultSize(width: 1180, height: 800)
        .windowStyle(.hiddenTitleBar)
        .commands {
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
    }
}
