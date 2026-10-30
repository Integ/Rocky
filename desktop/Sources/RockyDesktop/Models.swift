import Foundation

struct InferenceSettings: Codable, Equatable {
    var model: String
    var reasoning_effort: String?
    var provider: String?
    var base_url: String?
}

struct GoalContract: Codable, Equatable {
    var objective: String
    var acceptance_criteria: [String]
    var deliverables: [Deliverable]
    var allowed_tools: [String] = ["read_file", "write_file", "list_files", "fetch_url", "http_request", "run_command", "append_file", "edit_file", "browse"]
    var allowed_domains: [String] = []
    /// Granted folders; nil on tasks submitted before folders existed.
    var folders: [String]? = nil
    /// Attached reference files, readable without approval.
    var files: [String]? = nil
    var limits: TaskLimits?

    /// The same task with each finite limit doubled, up to `TaskLimits.retryCeiling`.
    /// Disabled (nil) limits stay disabled.
    func retryingWithLargerBudget() -> GoalContract {
        var copy = self
        let current = limits ?? TaskLimits()
        let ceiling = TaskLimits.retryCeiling
        func grow(_ value: Int?, _ maximum: Int?) -> Int? {
            guard let value, let maximum else { return value }
            return max(value, min(maximum, value > Int.max / 2 ? Int.max : value * 2))
        }
        copy.limits = TaskLimits(
            max_steps: grow(current.max_steps, ceiling.max_steps),
            max_model_calls: grow(current.max_model_calls, ceiling.max_model_calls),
            max_tokens: grow(current.max_tokens, ceiling.max_tokens),
            timeout_seconds: grow(current.timeout_seconds, ceiling.timeout_seconds)
        )
        return copy
    }
}

/// A drafted contract and what the owner should check before submitting it.
struct ContractReview: Codable, Equatable {
    var contract: GoalContract
    var warnings: [ContractWarning]
    /// Tokens the drafting call used; not charged to the task.
    var drafting_tokens: Int?
}

struct ContractWarning: Codable, Equatable, Hashable {
    var code: String
    var message: String = ""
    var current: Int?
    var suggested: Int?

    var isConcern: Bool { code == "concern" }
    var text: String {
        switch code {
        case "concern": return message
        case "model_calls_low":
            return "模型调用上限 \(current ?? 0) 次没有给独立审查和修补留余量，建议至少 \(suggested ?? 0) 次。"
        case "token_budget_low":
            return "Token 上限 \(TaskLimits.tokens(current)) 偏低：每次调用都会重发上下文，按调用上限估算需要约 \(TaskLimits.tokens(suggested))。"
        case "time_limit_low":
            return "时限 \(TaskLimits.duration(current)) 可能不够用完调用上限，建议至少 \(TaskLimits.duration(suggested))。等你审批或补充信息的时间不计入时限。"
        default: return message.isEmpty ? "预算可能不足（\(code)）。" : message
        }
    }
}

struct TaskLimits: Codable, Equatable {
    var max_steps: Int? = 100
    var max_model_calls: Int? = 150
    var max_tokens: Int? = 500_000
    var timeout_seconds: Int? = 3600

    /// Owner-initiated retries may exceed the automatic estimate ceilings, but stay bounded.
    static let retryCeiling = TaskLimits(max_steps: 400, max_model_calls: 600, max_tokens: 20_000_000, timeout_seconds: 4 * 3600)

    /// The same limits with steps, calls and tokens scaled, up to `retryCeiling`; the time limit is kept.
    func scaledBudget(by factor: Int) -> TaskLimits {
        let ceiling = TaskLimits.retryCeiling
        func scale(_ value: Int?, _ maximum: Int?) -> Int? {
            guard let value, let maximum, factor > 1 else { return value }
            return max(value, min(maximum, value > Int.max / factor ? Int.max : value * factor))
        }
        return TaskLimits(max_steps: scale(max_steps, ceiling.max_steps), max_model_calls: scale(max_model_calls, ceiling.max_model_calls),
                          max_tokens: scale(max_tokens, ceiling.max_tokens), timeout_seconds: timeout_seconds)
    }

    static func duration(_ seconds: Int?) -> String {
        guard let seconds else { return "不限" }
        if seconds % 3600 == 0 { return "\(seconds / 3600) 小时" }
        if seconds % 60 == 0 { return "\(seconds / 60) 分钟" }
        return seconds > 60 ? "约 \(seconds / 60 + 1) 分钟" : "\(seconds) 秒"
    }

    static func tokens(_ value: Int?) -> String {
        guard let value else { return "不限" }
        if value >= 1_000_000 { return String(format: "%.1f 百万", Double(value) / 1_000_000) }
        if value >= 10_000 { return "\(value / 10_000) 万" }
        return "\(value)"
    }

    enum CodingKeys: String, CodingKey { case max_steps, max_model_calls, max_tokens, timeout_seconds }
    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(max_steps, forKey: .max_steps)
        try values.encode(max_model_calls, forKey: .max_model_calls)
        try values.encode(max_tokens, forKey: .max_tokens)
        try values.encode(timeout_seconds, forKey: .timeout_seconds)
    }
}

extension GoalContract {
    /// The service requires 1–20 nonblank criteria and deliverables.
    var isSubmittable: Bool {
        let blank = { (text: String) in text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        return (1...20).contains(acceptance_criteria.count) && !acceptance_criteria.contains(where: blank)
            && (1...20).contains(deliverables.count) && !deliverables.map(\.path).contains(where: blank)
    }
}

struct Deliverable: Codable, Equatable {
    var path: String
    var min_bytes: Int? = nil
    var contains: [String]? = nil
}

struct Artifact: Codable, Identifiable, Equatable {
    var path: String
    var size: Int
    var sha256: String
    var id: String { path }
}

struct ToolObservation: Codable, Equatable, Hashable {
    var step: Int
    var tool: String
    var success: Bool
    var content: String
    var progress_key: String?

    var toolLabel: String {
        switch tool {
        case "read_file": "读取文件"
        case "write_file": "写入文件"
        case "append_file": "追加文件"
        case "edit_file": "修改文件"
        case "list_files": "浏览文件"
        case "fetch_url": "读取网页"
        case "http_request": "网络请求"
        case "browse": "浏览器"
        case "run_command": "执行命令"
        case "verify": "结果核验"
        default: tool
        }
    }

    fileprivate var identity: ToolObservation {
        var value = self
        value.progress_key = progress_key ?? ""
        return value
    }

    fileprivate func isEquivalentOperation(to other: ToolObservation) -> Bool {
        var value = identity
        value.step = other.step
        return value == other.identity
    }
}

struct RunState: Codable, Equatable {
    var status: String
    var reason: String
    var steps: Int
    var model_calls: Int
    var input_tokens: Int
    var output_tokens: Int
    var artifacts: [Artifact]
    var updated_at: String
    var revision: Int
    var observations: [ToolObservation]?
    var wake_at: String?
    var pending_approval: PendingApproval?
    /// Requests declined because no one answered in time; the owner may still approve them.
    var deferred_approvals: [PendingApproval]?

    var isTerminal: Bool {
        ["succeeded", "failed", "cancelled", "budget_exhausted", "deadline_exceeded"].contains(status)
    }
    /// The reason in the owner's language. Fixed system messages are English
    /// on the service; the model's own words are shown as written.
    var displayReason: String { Self.localized(reason) }

    static func localized(_ reason: String) -> String {
        let text = reason.trimmingCharacters(in: .whitespacesAndNewlines)
        if let match = text.firstMatch(of: #/^(\d+) approval requests in a row went unanswered/#) {
            return "连续 \(match.1) 次审批无人回应，任务已暂停。可在“审批”里补批，或发消息让它继续。"
        }
        for (prefix, message) in systemReasons where text.hasPrefix(prefix) { return message }
        return reason
    }

    private static let systemReasons: [(String, String)] = [
        ("Owner approval is required", "这项操作需要你批准，批准前不会执行。"),
        ("The task's planning-step limit has been reached", "已达到步骤上限。可以加倍预算继续。"),
        ("The task's model-call limit has been reached", "已达到模型调用上限。可以加倍预算继续。"),
        ("The task's cumulative token limit has been reached", "已达到 token 上限。可以加倍预算继续。"),
        ("The task's wall-clock limit has elapsed", "已到任务时限。可以延长时限继续。"),
        ("Cancelled by the user", "任务已停止。"),
        ("Execution cancelled", "任务已停止。"),
        ("Continuing from saved progress with a larger budget", "已提高上限，正在从保存的进度继续。"),
        ("Continuing with the owner's follow-up request", "正在按你的追加要求继续。"),
        ("Recovering from saved progress", "正在从保存的进度恢复。"),
        ("Recovery did not produce new progress", "重试没有带来新进展。请补充信息或解决依赖后发消息继续。"),
        ("The action could not complete", "操作没有完成，Rocky 会先确认实际状态再换办法。"),
        ("The reflection observer failed", "扩展检查失败，请检查扩展的依赖。"),
        ("External input received", "收到你的消息，继续执行。"),
    ]

    /// An ended task the owner can continue with a follow-up request.
    var acceptsFollowUp: Bool {
        ["succeeded", "cancelled", "budget_exhausted", "deadline_exceeded"].contains(status)
    }
    var statusLabel: String {
        switch status {
        case "queued": "等待执行"
        case "running": "正在工作"
        case "waiting": "定时等待"
        case "blocked": "需要你的回复"
        case "succeeded": "已完成"
        case "failed": "执行失败"
        case "cancelled": "已停止"
        case "budget_exhausted": "已达预算上限"
        case "deadline_exceeded": "已到截止时间"
        default: "状态未知"
        }
    }
}

struct PendingApproval: Codable, Equatable {
    var fingerprint: String
    var operation_id: String
    var action: ProposedAction
    var reason: String
    /// Leading command words the owner may approve for the rest of the task.
    var command_prefix: [String]?
    /// HTTP method and origin the owner may approve for the rest of the task.
    var http_scope: HttpScope?
    /// When an unanswered request is declined; nil waits for the owner.
    var expires_at: String?

    var commandPrefixText: String? {
        guard let command_prefix, !command_prefix.isEmpty else { return nil }
        return command_prefix.joined(separator: " ")
    }
    /// What a task-wide or permanent approval of this request would cover.
    var reusableScopeText: String? {
        if let prefix = commandPrefixText { return "以 \(prefix) 开头的简单命令" }
        if let http_scope { return "\(http_scope.method) \(http_scope.origin)" }
        return nil
    }
    var expiryDate: Date? {
        guard let expires_at else { return nil }
        // Python writes microseconds; whole seconds are precise enough here.
        let trimmed = expires_at.replacingOccurrences(of: #"\.[0-9]+"#, with: "", options: .regularExpression)
        return ISO8601DateFormatter().date(from: trimmed)
    }
    var summary: String {
        [action.tool, action.path, action.url, action.content, action.runModeText].compactMap { $0 }.joined(separator: "\n")
    }
}

struct HttpScope: Codable, Equatable {
    var method: String
    var origin: String
}

/// How the owner answers an approval request.
enum ApprovalScope: String {
    case action, task, always, deny, deferred, dismiss
}

/// One pending or declined request in the cross-task approval list.
struct ApprovalItem: Identifiable, Equatable {
    var goal: Goal
    var approval: PendingApproval
    var deferred: Bool
    var id: String { "\(goal.id):\(approval.fingerprint)" }
}

struct ApprovalSettings: Codable, Equatable {
    var read_only_commands: Bool
    var timeout_minutes: Int
}

struct ApprovalRule: Codable, Equatable, Identifiable {
    var rule_id: String
    var kind: String
    var command: String?
    var methods: [String]
    var origin: String?
    var path_prefix: String
    var id: String { rule_id }
    var summary: String {
        kind == "command" ? "命令 \(command ?? "")" : "\(methods.joined(separator: "/")) \(origin ?? "")\(path_prefix == "/" ? "" : path_prefix)"
    }
}

struct ApprovalPolicy: Codable, Equatable {
    var settings: ApprovalSettings
    var rules: [ApprovalRule]
    var allowlist: [String]
}

struct ProposedAction: Codable, Equatable {
    var tool: String
    var path: String?
    var url: String?
    var content: String?
    /// false: the command runs outside Seatbelt with the owner's full privileges.
    var sandbox: Bool?
    /// run_command only: a longer limit than the default 30 seconds.
    var timeout_seconds: Int?
    /// run_command only: starts a job that keeps running after the call returns.
    var background: Bool?

    var isUnsandboxed: Bool { tool == "run_command" && sandbox == false }
    /// How the command runs, when it differs from a normal 30-second command.
    var runModeText: String? {
        if background == true { return "后台作业：立即返回，任务结束时停止" }
        if let timeout_seconds { return "最长运行 \(TaskLimits.duration(timeout_seconds))" }
        return nil
    }
}

struct Goal: Codable, Identifiable, Equatable {
    var goal_id: String
    var contract: GoalContract
    var state: RunState
    var created_at: String
    var model_settings: InferenceSettings?
    var archived: Bool?
    var id: String { goal_id }
    var isArchived: Bool { archived == true }
    var title: String { String(contract.objective.split(separator: "\n").first.map(String.init)?.prefix(60) ?? "新任务".prefix(60)) }
}

struct GoalEvent: Decodable, Identifiable {
    var event_id: Int
    var state: RunState
    var id: Int { event_id }
}

struct ExecutionEntry: Identifiable {
    var id: Int
    var status: String
    var statusLabel: String
    var reason: String
    var displayReason: String?
    var observation: ToolObservation?
    var artifacts: [Artifact]
    var artifactsChanged: Bool
    var wakeAt: String?
    var firstStep: Int?
    var lastStep: Int?
    var count = 1
    var firstTimestamp: String
    var lastTimestamp: String

    var title: String { displayReason ?? (reason.isEmpty ? statusLabel : reason) }
    var stepLabel: String {
        guard let firstStep, let lastStep else { return "状态" }
        return firstStep == lastStep ? "第 \(firstStep) 步" : "第 \(firstStep)–\(lastStep) 步"
    }

    static func build(from events: [GoalEvent]) -> [ExecutionEntry] {
        var entries: [ExecutionEntry] = []
        var seenObservations = Set<ToolObservation>()
        var previous: Snapshot?
        for event in events {
            let state = event.state
            let snapshot = Snapshot(state)
            // Dispatch snapshots carry the prior result; only new observations own a step.
            // A parallel read batch records several observations for one step at once.
            let lastStep = state.observations?.last?.step
            let fresh = (state.observations ?? []).filter {
                $0.step == lastStep && seenObservations.insert($0.identity).inserted
            }
            guard !fresh.isEmpty || snapshot != previous else { continue }
            for (index, observation) in (fresh.isEmpty ? [nil] : fresh.map(Optional.some)).enumerated() {
                let entry = ExecutionEntry(
                    // Extra batch entries get negative IDs, which never collide with event IDs.
                    id: index == 0 ? event.id : -(event.id * 16 + index),
                    status: state.status, statusLabel: state.statusLabel,
                    reason: snapshot.reason,
                    displayReason: state.status == "running" && ["waiting", "blocked"].contains(previous?.status ?? "") && snapshot.reason == previous?.reason && observation == nil ? "恢复执行" : nil,
                    observation: observation, artifacts: snapshot.artifacts,
                    artifactsChanged: index == 0 && snapshot.artifacts != (previous?.artifacts ?? []),
                    wakeAt: state.wake_at, firstStep: observation?.step, lastStep: observation?.step,
                    firstTimestamp: state.updated_at, lastTimestamp: state.updated_at
                )
                if let observation, let last = entries.last,
                   last.status == entry.status, last.reason == entry.reason,
                   last.artifacts == entry.artifacts, last.wakeAt == entry.wakeAt,
                   observation.step > (last.lastStep ?? 0),
                   last.observation?.isEquivalentOperation(to: observation) == true {
                    entries[entries.count - 1].count += 1
                    entries[entries.count - 1].lastStep = observation.step
                    entries[entries.count - 1].lastTimestamp = state.updated_at
                } else {
                    entries.append(entry)
                }
            }
            previous = snapshot
        }
        return entries
    }

    private struct Snapshot: Equatable {
        var status: String
        var reason: String
        var artifacts: [Artifact]
        var wakeAt: String?

        init(_ state: RunState) {
            status = state.status
            reason = state.reason.split(whereSeparator: \.isWhitespace).joined(separator: " ")
            artifacts = state.artifacts.sorted {
                if $0.path != $1.path { return $0.path < $1.path }
                if $0.sha256 != $1.sha256 { return $0.sha256 < $1.sha256 }
                return $0.size < $1.size
            }
            wakeAt = state.wake_at
        }
    }
}

struct FeedbackMessage: Decodable, Identifiable {
    var command_id: String
    var message: String
    var sent: Bool
    var id: String { command_id }
}

struct ModelOption: Decodable, Identifiable, Equatable {
    var id: String
    var label: String
    var reasoning_efforts: [String] = []

    static func choices(from catalog: [ModelOption], preserving selectedID: String) -> [ModelOption] {
        var seen = Set<String>()
        var choices = catalog.filter { !$0.id.isEmpty && seen.insert($0.id).inserted }
        if !selectedID.isEmpty && !seen.contains(selectedID) {
            choices.append(ModelOption(id: selectedID, label: selectedID))
        }
        return choices
    }

    func normalizedReasoning(_ effort: String?) -> String? {
        guard let effort, reasoning_efforts.contains(effort) else { return nil }
        return effort
    }
}

func reasoningLabel(_ effort: String?) -> String {
    switch effort {
    case nil, "": "模型默认"
    case "none": "不启用推理（none）"
    case "minimal": "最少（minimal）"
    case "low": "低（low）"
    case "medium": "中（medium）"
    case "high": "高（high）"
    case "xhigh": "超高（xhigh）"
    case "max": "最高（max）"
    case "ultra": "极高（ultra）"
    case .some(let value): value
    }
}

private func normalizedModelEndpoint(_ value: String) -> String {
    value.trimmingCharacters(in: .whitespacesAndNewlines)
        .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
}

struct ProviderPreset: Decodable, Identifiable {
    var id: String
    var label: String
    var base_url: String
    var model: String
    var requires_api_key: Bool
    var preset: String?
    var models: [ModelOption]?
    var selectionID: String { preset ?? id }
    var modelOptions: [ModelOption] { ModelOption.choices(from: models ?? [], preserving: model) }
}

struct ProviderSettings: Decodable {
    var provider: String
    var base_url: String?
    var model: String
    var reasoning_effort: String?
    var has_api_key: Bool
    var credential_storage: String
    var credential_error: String?
    var providers: [ProviderPreset]
    var models: [ModelOption]?
    var isConfigured: Bool { provider == "ollama" || has_api_key }
    var label: String {
        if let match = providers.first(where: { $0.id == provider && $0.base_url.trimmingCharacters(in: CharacterSet(charactersIn: "/")) == (base_url ?? "").trimmingCharacters(in: CharacterSet(charactersIn: "/")) }) { return match.label }
        return providers.first(where: { $0.id == provider })?.label ?? provider
    }

    func modelOptions(provider draftProvider: String, baseURL: String, selectedModel: String) -> [ModelOption] {
        let endpoint = normalizedModelEndpoint(baseURL)
        let matchingPreset = providers.first {
            $0.id == draftProvider && normalizedModelEndpoint($0.base_url) == endpoint
        } ?? (draftProvider == "openai-compatible" ? nil : providers.first { $0.id == draftProvider })
        var catalog = matchingPreset?.modelOptions ?? []
        if draftProvider == provider && endpoint == normalizedModelEndpoint(base_url ?? "") {
            catalog = ModelOption.choices(from: models ?? catalog, preserving: model)
        }
        return ModelOption.choices(from: catalog, preserving: selectedModel)
    }
}

struct ProviderUpdate: Encodable {
    var provider: String
    var base_url: String
    var model: String
    var reasoning_effort: String?
    var api_key: String?
    var clear_api_key: Bool = false
}

struct ArtifactContent: Decodable {
    var path: String
    var content: String
    var truncated: Bool
}

struct GoalDraft: Encodable, Equatable {
    var objective: String
    /// Folders the owner grants this task to change without per-file approval.
    var folders: [String]
    /// Files attached as reference material.
    var files: [String]

    init(objective: String, folders: [String] = [], files: [String] = []) throws {
        let objective = objective.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !objective.isEmpty, objective.count <= 8000 else { throw InputError.invalid("请填写目标，最多 8,000 个字符。") }
        guard folders.count <= 8 else { throw InputError.invalid("一个任务最多授权 8 个文件夹。") }
        guard files.count <= 20 else { throw InputError.invalid("一个任务最多附加 20 个文件。") }
        self.objective = objective
        self.folders = folders
        self.files = files
    }

    private enum CodingKeys: String, CodingKey { case objective, folders, files }

    /// A draft without grants or attachments sends only its objective.
    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(objective, forKey: .objective)
        if !folders.isEmpty { try values.encode(folders, forKey: .folders) }
        if !files.isEmpty { try values.encode(files, forKey: .files) }
    }
}

enum InputError: LocalizedError {
    case invalid(String)
    var errorDescription: String? { if case .invalid(let message) = self { message } else { nil } }
}

extension String {
    var lines: [String] { split(separator: "\n").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty } }
}

/// A lasting fact saved for future tasks.
struct Memory: Codable, Identifiable, Equatable {
    var key: String
    var kind: String
    var content: String
    var revision: Int
    var source: String?
    var id: String { key }
}

/// A memory Rocky suggested; nothing is kept until the owner saves it.
struct MemoryProposal: Codable, Identifiable, Equatable {
    var proposal_id: String
    var goal_id: String
    var key: String
    var kind: String
    var content: String
    var replaces: String?
    var id: String { proposal_id }

    static let kinds = ["preference", "decision", "correction", "pitfall"]
    static func label(_ kind: String) -> String {
        switch kind {
        case "preference": "偏好"
        case "decision": "决定"
        case "correction": "更正"
        case "pitfall": "注意事项"
        default: kind
        }
    }
    var kindLabel: String { Self.label(kind) }
}
