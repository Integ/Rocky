import Foundation

struct TriggerRuleRequest: Encodable {
    var name: String
    var source: String
    var match: String
    var mode: String = "observe"
    var contract: GoalContract
    var debounce_seconds = 30
    var daily_auto_limit = 5
    var daily_timezone = "UTC"
}

struct TriggerRule: Codable, Identifiable, Equatable {
    var rule_id: String
    var name: String
    var source: String
    var match: String
    var mode: String
    var contract: GoalContract
    var revision: Int
    var problem: String
    var last_goal_id: String?
    var daily_auto_limit: Int?
    var daily_timezone: String?
    var auto_runs_today: Int?
    var quota_resets_at: String?
    var id: String { rule_id }
    var modeLabel: String { Self.label(mode) }
    static func label(_ mode: String) -> String {
        switch mode { case "auto": "自动执行"; case "observe": "仅观察"; default: "关闭" }
    }
}

struct TriggerEvent: Codable, Equatable {
    var event_id: String
    var name: String
    var summary: String
}

struct TriggerDecision: Codable, Identifiable, Equatable {
    var decision_id: String
    var rule_id: String
    var rule_name: String
    var rule_revision: Int
    var event: TriggerEvent
    var mode: String
    var outcome: String
    var received_at: String
    var goal_id: String?
    var reason: String
    var id: String { decision_id }
    var needsOwner: Bool { ["observed", "skipped_busy", "unavailable", "quota_limited"].contains(outcome) }
    var statusLabel: String {
        switch outcome {
        case "started": "已创建任务"
        case "dismissed": "已忽略"
        case "skipped_busy": "上次任务未结束，本次未启动"
        case "unavailable": "需要检查文件"
        case "quota_limited": "今日自动启动已达上限"
        default: "建议执行，尚未创建任务"
        }
    }
}

struct TriggerDecisionPage: Codable, Equatable {
    var items: [TriggerDecision]
    var total: Int
    var next_cursor: String?
}

struct TriggerFeedState {
    var items: [TriggerDecision] = []
    var total = 0
    var cursor: String?
    var nextCursor: String?
    var previousCursors: [String?] = []

    mutating func apply(_ page: TriggerDecisionPage) {
        items = page.items
        total = page.total
        nextCursor = page.next_cursor
    }
}
