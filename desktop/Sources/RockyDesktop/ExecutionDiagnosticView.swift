import SwiftUI

struct ModelQueueStatus: Codable, Equatable {
    var capacity: Int
    var reserved_interactive: Int
    var running: Int
    var waiting: Int
    var interactive_waiting: Int
}

struct DiagnosticBudget: Codable, Equatable {
    var steps_remaining: Int?
    var model_calls_remaining: Int?
    var tokens_remaining: Int?
    var wall_seconds_remaining: Double?
    var timer_paused: Bool
}

struct GoalDiagnostic: Codable, Equatable {
    var goal_id: String
    var revision: Int
    var status: String
    var stage: String
    var title: String
    var detail: String
    var since: String?
    var observed_at: String
    var retry_at: String?
    var attempt: Int?
    var priority: String?
    var queue_wait_seconds: Double?
    var model_queue: ModelQueueStatus
    var budget: DiagnosticBudget

    static func date(_ value: String?) -> Date? {
        guard let value else { return nil }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return fractional.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    }

    func elapsed(at now: Date) -> Double? { Self.date(since).map { max(0, now.timeIntervalSince($0)) } }
    func remainingTime(at now: Date) -> Double? {
        guard let remaining = budget.wall_seconds_remaining else { return nil }
        return max(0, remaining - max(0, now.timeIntervalSince(Self.date(observed_at) ?? now)))
    }
    static func duration(_ seconds: Double) -> String {
        let value = Int(max(0, seconds))
        if value < 60 { return "\(value) 秒" }
        if value < 3600 { return "\(value / 60) 分 \(value % 60) 秒" }
        return "\(value / 3600) 小时 \(value % 3600 / 60) 分"
    }
}

struct ExecutionDiagnosticView: View {
    let diagnostic: GoalDiagnostic

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Label(diagnostic.title, systemImage: diagnostic.stage == "retry" ? "arrow.clockwise" : "waveform.path")
                        .font(.callout.weight(.medium)).foregroundStyle(Color.rockyAccent)
                    Spacer()
                    if let elapsed = diagnostic.elapsed(at: context.date), !["succeeded", "cancelled", "failed", "budget_exhausted", "deadline_exceeded"].contains(diagnostic.status) {
                        Text("已持续 " + GoalDiagnostic.duration(elapsed)).font(.caption).foregroundStyle(.secondary)
                    }
                }
                Text(diagnostic.detail).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if diagnostic.priority == "interactive", ["model", "model_queued", "retry"].contains(diagnostic.stage) {
                    Text("优先处理你的消息").font(.caption).foregroundStyle(Color.rockyAccent)
                }
                if let attempt = diagnostic.attempt, attempt > 1 {
                    Text("第 \(attempt) 次尝试").font(.caption).foregroundStyle(.secondary)
                }
                if let retry = GoalDiagnostic.date(diagnostic.retry_at), diagnostic.stage == "retry" {
                    Text(retry > context.date ? "预计 " + GoalDiagnostic.duration(retry.timeIntervalSince(context.date)) + "后可重试" : "等待执行引擎安排重试")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Text("预算余量：步骤 \(diagnostic.budget.steps_remaining.map(String.init) ?? "不限") · 模型调用 \(diagnostic.budget.model_calls_remaining.map(String.init) ?? "不限") · \(TaskLimits.tokens(diagnostic.budget.tokens_remaining)) tokens")
                    .font(.caption).foregroundStyle(.secondary)
                if diagnostic.budget.timer_paused {
                    Text("等你处理期间，任务计时已暂停").font(.caption).foregroundStyle(.secondary)
                } else if let remaining = diagnostic.remainingTime(at: context.date) {
                    Text("可用运行时间 " + GoalDiagnostic.duration(remaining)).font(.caption).foregroundStyle(.secondary)
                }
                if diagnostic.stage == "model_queued" {
                    Text("全局模型调用 \(diagnostic.model_queue.running)/\(diagnostic.model_queue.capacity) · 排队 \(diagnostic.model_queue.waiting) 个")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.rockyAccent.opacity(0.055), in: RoundedRectangle(cornerRadius: 10))
        }
    }
}

struct ModelQueueSection: View {
    let api: APIClient
    @State private var status: ModelQueueStatus?
    @State private var error: String?

    var body: some View {
        Section("模型调用调度") {
            if let status {
                Text("正在调用 \(status.running)/\(status.capacity) · 排队 \(status.waiting) · 人工请求排队 \(status.interactive_waiting)").font(.callout)
                Text("预留 \(status.reserved_interactive) 个名额处理人工追加消息和手动拟定要求；合同生成、议程与任务执行共享总限额。正在执行的请求会继续完成。")
                    .font(.caption).foregroundStyle(.secondary)
            } else if let error { Text(error).font(.caption).foregroundStyle(.secondary) }
            else { ProgressView().controlSize(.small) }
        }.task {
            while !Task.isCancelled {
                do { status = try await api.modelQueue(); error = nil }
                catch { status = nil; self.error = error.localizedDescription }
                do { try await Task.sleep(for: .seconds(5)) } catch { break }
            }
        }
    }
}
