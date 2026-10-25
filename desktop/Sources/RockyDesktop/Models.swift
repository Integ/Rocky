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
    var allowed_tools: [String] = ["read_file", "write_file", "list_files", "fetch_url", "http_request", "run_command", "append_file", "edit_file"]
    var allowed_domains: [String] = []
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

struct TaskLimits: Codable, Equatable {
    var max_steps: Int? = 100
    var max_model_calls: Int? = 150
    var max_tokens: Int? = 500_000
    var timeout_seconds: Int? = 3600

    /// Owner-initiated retries may exceed the automatic estimate ceilings, but stay bounded.
    static let retryCeiling = TaskLimits(max_steps: 400, max_model_calls: 600, max_tokens: 20_000_000, timeout_seconds: 4 * 3600)

    enum CodingKeys: String, CodingKey { case max_steps, max_model_calls, max_tokens, timeout_seconds }
    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(max_steps, forKey: .max_steps)
        try values.encode(max_model_calls, forKey: .max_model_calls)
        try values.encode(max_tokens, forKey: .max_tokens)
        try values.encode(timeout_seconds, forKey: .timeout_seconds)
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

    var isTerminal: Bool {
        ["succeeded", "failed", "cancelled", "budget_exhausted", "deadline_exceeded"].contains(status)
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

    var commandPrefixText: String? {
        guard let command_prefix, !command_prefix.isEmpty else { return nil }
        return command_prefix.joined(separator: " ")
    }
}

struct ProposedAction: Codable, Equatable {
    var tool: String
    var path: String?
    var url: String?
    var content: String?
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

    init(objective: String) throws {
        let objective = objective.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !objective.isEmpty, objective.count <= 8000 else { throw InputError.invalid("请填写目标，最多 8,000 个字符。") }
        self.objective = objective
    }
}

enum InputError: LocalizedError {
    case invalid(String)
    var errorDescription: String? { if case .invalid(let message) = self { message } else { nil } }
}

extension String {
    var lines: [String] { split(separator: "\n").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty } }
}
