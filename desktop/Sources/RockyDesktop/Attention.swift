import AppKit
import Foundation
import UserNotifications

/// Something a task needs the owner to know about.
struct AttentionAlert: Equatable {
    enum Kind: Equatable {
        case approval(summary: String, unsandboxed: Bool)
        case blocked(reason: String)
        /// The task has waited for the owner since the given time.
        case reminder(since: Date)
        case finished(status: String, label: String)
    }

    var goalID: String
    var goalTitle: String
    var kind: Kind

    /// One notification per task and kind; a newer one replaces the older.
    var identifier: String {
        switch kind {
        case .approval, .blocked, .reminder: "attention:\(goalID)"
        case .finished: "finished:\(goalID)"
        }
    }

    var title: String {
        switch kind {
        case .approval(_, let unsandboxed): unsandboxed ? "需要批准（沙箱外运行）" : "需要你批准一项操作"
        case .blocked: "Rocky 需要你的回复"
        case .reminder: "任务仍在等你处理"
        case .finished(let status, let label): status == "succeeded" ? "任务已完成" : "任务已结束：\(label)"
        }
    }

    var body: String {
        let task = "“\(goalTitle)”"
        switch kind {
        case .approval(let summary, _): return "\(task)\n\(summary.prefix(160))"
        case .blocked(let reason): return reason.isEmpty ? task : "\(task)\n\(reason.prefix(160))"
        case .reminder(let since):
            let hours = max(1, Int(Date().timeIntervalSince(since) / 3600))
            return "\(task) 已等待约 \(hours) 小时。"
        case .finished: return task
        }
    }

    var playsSound: Bool {
        if case .finished = kind { return false }
        return true
    }
}

/// Turns successive task lists into alerts: a new approval request, a task
/// that starts waiting for the owner, a reminder while it keeps waiting, and
/// a task that ends. The first list only sets the baseline, so relaunching
/// the app does not repeat old alerts.
struct AttentionTracker {
    struct Entry: Equatable {
        var status: String
        var approval: String?
        var waitingSince: Date?
        var lastAlert: Date?
    }

    /// How long a waiting task goes before it is mentioned again.
    var reminderInterval: TimeInterval = 3 * 3600
    private(set) var entries: [String: Entry] = [:]
    private var primed = false

    static func needsOwner(_ state: RunState) -> Bool { state.status == "blocked" }

    mutating func update(_ goals: [Goal], now: Date = Date()) -> [AttentionAlert] {
        var alerts: [AttentionAlert] = []
        var next: [String: Entry] = [:]
        for goal in goals where !goal.isArchived {
            let state = goal.state
            let previous = entries[goal.id]
            let waiting = Self.needsOwner(state)
            var entry = Entry(
                status: state.status,
                approval: state.pending_approval?.fingerprint,
                waitingSince: waiting ? (previous?.waitingSince ?? now) : nil,
                lastAlert: waiting ? previous?.lastAlert : nil
            )
            var alert: AttentionAlert.Kind?
            if !primed {
                // Waiting tasks found at launch are reminded about later, not now.
                if waiting { entry.lastAlert = now }
            } else if let approval = state.pending_approval, approval.fingerprint != previous?.approval {
                alert = .approval(summary: approval.summary, unsandboxed: approval.action.isUnsandboxed)
            } else if waiting, previous?.status != "blocked" {
                alert = .blocked(reason: state.displayReason)
            } else if waiting, let last = entry.lastAlert, now.timeIntervalSince(last) >= reminderInterval {
                alert = .reminder(since: entry.waitingSince ?? last)
            } else if state.isTerminal, let previous, !Self.terminal(previous.status) {
                alert = .finished(status: state.status, label: state.statusLabel)
            }
            if let alert {
                if waiting { entry.lastAlert = now }
                alerts.append(AttentionAlert(goalID: goal.id, goalTitle: goal.title, kind: alert))
            }
            next[goal.id] = entry
        }
        entries = next
        primed = true
        return alerts
    }

    private static func terminal(_ status: String) -> Bool {
        ["succeeded", "failed", "cancelled", "budget_exhausted", "deadline_exceeded"].contains(status)
    }
}

extension NSApplication {
    /// The workspace window, if it is still open (possibly hidden or minimized).
    var mainWorkspaceWindow: NSWindow? {
        windows.first { $0.identifier?.rawValue.hasPrefix("main") == true }
            ?? windows.first { $0.canBecomeMain && $0.styleMask.contains(.titled) }
    }
}

/// Where alerts go; the app uses system notifications and the Dock badge.
@MainActor
protocol AttentionDelivery: AnyObject {
    var onOpen: (@MainActor (String) -> Void)? { get set }
    func deliver(_ alerts: [AttentionAlert])
    func updateBadge(_ count: Int)
}

@MainActor
final class SystemNotifier: NSObject, AttentionDelivery, UNUserNotificationCenterDelegate {
    var onOpen: (@MainActor (String) -> Void)?
    private let center = UNUserNotificationCenter.current()

    /// Notifications need a bundled app; `swift run` and tests have none.
    static func makeIfAvailable() -> SystemNotifier? {
        guard Bundle.main.bundleURL.pathExtension == "app", Bundle.main.bundleIdentifier != nil else { return nil }
        return SystemNotifier()
    }

    private override init() {
        super.init()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    func deliver(_ alerts: [AttentionAlert]) {
        for alert in alerts {
            let content = UNMutableNotificationContent()
            content.title = alert.title
            content.body = alert.body
            content.threadIdentifier = alert.goalID
            content.userInfo = ["goal_id": alert.goalID]
            if alert.playsSound { content.sound = .default }
            center.add(UNNotificationRequest(identifier: alert.identifier, content: content, trigger: nil))
        }
    }

    func updateBadge(_ count: Int) {
        NSApplication.shared.dockTile.badgeLabel = count > 0 ? "\(count)" : nil
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let goalID = response.notification.request.content.userInfo["goal_id"] as? String
        completionHandler()
        guard let goalID else { return }
        Task { @MainActor in self.onOpen?(goalID) }
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        // The owner may be looking at another task, so still show a banner.
        completionHandler([.banner, .list])
    }
}
