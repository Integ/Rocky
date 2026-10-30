import Foundation

/// One step of a long-running plan as drafted or edited; becomes a task when its turn comes.
struct PlanStepDraft: Codable, Equatable, Identifiable {
    var title: String
    var objective: String
    /// Local identity for editing lists; not sent.
    var id = UUID()

    init(title: String, objective: String) {
        self.title = title
        self.objective = objective
    }

    private enum CodingKeys: String, CodingKey { case title, objective }
}

/// The AI's proposed breakdown, shown before anything starts.
struct PlanDraftReview: Decodable, Equatable {
    var steps: [PlanStepDraft]
    var concerns: [String]
    var drafting_tokens: Int?
}

struct PlanRequest: Encodable {
    var objective: String
    var steps: [PlanStepDraft]
    var folders: [String] = []
    var files: [String] = []
    var replan = true
}

struct PlanStep: Codable, Identifiable, Equatable {
    var step_id: String
    var title: String
    var objective: String
    var status: String
    var goal_id: String?
    var attempts: Int?
    var summary: String?
    var outputs: [String]?
    var id: String { step_id }

    var statusLabel: String {
        switch status {
        case "pending": "等待"
        case "running": "进行中"
        case "succeeded": "已完成"
        case "skipped": "已跳过"
        case "failed": "未完成"
        default: status
        }
    }
    var symbol: String {
        switch status {
        case "running": "circle.dotted.circle"
        case "succeeded": "checkmark.circle.fill"
        case "skipped": "arrow.uturn.right.circle"
        case "failed": "exclamationmark.circle.fill"
        default: "circle"
        }
    }
}

/// A large goal Rocky works through step by step, each step as its own task.
struct Plan: Codable, Identifiable, Equatable {
    var plan_id: String
    var objective: String
    var status: String
    var reason: String
    var replan: Bool?
    var steps: [PlanStep]
    var notes: [String]?
    var review_pending: Bool?
    var planning_tokens: Int?
    var created_at: String
    var updated_at: String
    var id: String { plan_id }

    var title: String { String(objective.split(separator: "\n").first.map(String.init)?.prefix(60) ?? "长期目标") }
    var isEnded: Bool { status == "succeeded" || status == "cancelled" }
    var needsOwner: Bool { status == "attention" }
    var doneCount: Int { steps.filter { $0.status == "succeeded" || $0.status == "skipped" }.count }
    var statusLabel: String {
        switch status {
        case "active": review_pending == true ? "正在检查后续步骤" : "进行中"
        case "paused": "已暂停"
        case "attention": "需要你处理"
        case "succeeded": "已完成"
        case "cancelled": "已停止"
        default: status
        }
    }
    var symbol: String {
        switch status {
        case "active": "arrow.triangle.2.circlepath"
        case "paused": "pause.circle"
        case "attention": "exclamationmark.triangle.fill"
        case "succeeded": "checkmark.seal.fill"
        default: "stop.circle"
        }
    }
}

/// When a scheduled task runs. Weekdays count from 1 = Monday.
struct Recurrence: Codable, Equatable {
    var kind: String
    var start_at: String?
    var every_minutes: Int?
    var time: String?
    var weekdays: [Int] = []
    var timezone: String = TimeZone.current.identifier

    static let weekdayNames = ["一", "二", "三", "四", "五", "六", "日"]

    var summary: String {
        switch kind {
        case "once":
            return "一次：" + (Recurrence.date(start_at)?.formatted(date: .abbreviated, time: .shortened) ?? "")
        case "interval":
            let minutes = every_minutes ?? 0
            if minutes % 1440 == 0 { return "每 \(minutes / 1440) 天" }
            if minutes % 60 == 0 { return "每 \(minutes / 60) 小时" }
            return "每 \(minutes) 分钟"
        case "daily":
            return "每天 \(time ?? "")"
        case "weekly":
            let days = weekdays.filter { (1...7).contains($0) }.map { "周" + Recurrence.weekdayNames[$0 - 1] }
            return days.joined(separator: "、") + " \(time ?? "")"
        default:
            return kind
        }
    }

    static func date(_ text: String?) -> Date? {
        guard let text else { return nil }
        let formatter = ISO8601DateFormatter()
        if let date = formatter.date(from: text) { return date }
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: text)
    }

    static func stamp(_ date: Date) -> String { ISO8601DateFormatter().string(from: date) }
}

/// A task contract Rocky starts once, at an interval, or daily/weekly.
struct Schedule: Codable, Identifiable, Equatable {
    var schedule_id: String
    var name: String
    var contract: GoalContract
    var recurrence: Recurrence
    var enabled: Bool
    var next_run_at: String?
    var last_run_at: String?
    var last_goal_id: String?
    var runs: Int
    var skipped: Int
    var id: String { schedule_id }

    var nextRun: Date? { Recurrence.date(next_run_at) }
    var statusText: String {
        if !enabled { return "已暂停" }
        guard let nextRun else { return "已结束" }
        return "下次：" + nextRun.formatted(date: .abbreviated, time: .shortened)
    }
    var historyText: String {
        var text = runs == 0 ? "还没有运行过" : "已运行 \(runs) 次"
        if skipped > 0 { text += "，\(skipped) 次因上次未结束而跳过" }
        return text
    }
}

struct ScheduleRequest: Encodable {
    var name: String
    var contract: GoalContract
    var recurrence: Recurrence
    var enabled = true
}

extension GoalContract {
    /// The contract without the note a scheduled run adds to its objective.
    var withoutScheduleNote: GoalContract {
        var copy = self
        if let range = copy.objective.range(of: "\n\n（这是定时任务“") {
            copy.objective = String(copy.objective[..<range.lowerBound])
        }
        return copy
    }
}
