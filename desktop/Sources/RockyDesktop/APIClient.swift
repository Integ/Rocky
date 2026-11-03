import Foundation

struct APIClient {
    let baseURL: URL
    let token: String
    var session: URLSession = .shared

    private struct ModelFailure: Decodable {
        struct Detail: Decodable { let code: String }
        let detail: Detail
    }

    private static func modelFailureMessage(_ data: Data) -> String {
        // Only allowlisted categories are shown. Provider response text can
        // contain credentials and must never become a user-facing message.
        let code = (try? JSONDecoder().decode(ModelFailure.self, from: data))?.detail.code
        switch code {
        case "authentication_failed": return "模型服务拒绝了 API 密钥，请在“设置与模型”中更新密钥。"
        case "missing_api_key": return "尚未配置模型 API 密钥，请打开“设置与模型”。"
        case "model_access_denied": return "当前密钥无法访问该模型或 API 地址，请检查模型权限和连接设置。"
        case "quota_exceeded": return "模型服务的余额或用量额度已耗尽，请检查服务商账户的计费和用量上限。"
        case "rate_limited": return "模型服务请求过于频繁，请稍后重试。"
        case "network_error": return "无法连接模型服务，请检查网络和 API 地址。"
        case "model_timeout": return "模型响应超时，请稍后重试。"
        case "provider_unavailable": return "模型服务暂时不可用，请稍后重试。"
        default: return "AI 未能完成任务评估，请检查模型配置后重新提交。"
        }
    }

    /// A server page and its validator. `nil` from a revalidated request means unchanged.
    struct GoalList: Equatable {
        var goals: [Goal]
        var tag: String?
    }

    private func rawRequest(_ path: String, method: String = "GET", body: Data? = nil, idempotencyKey: String? = nil, timeout: TimeInterval = 20) async throws -> Data {
        try await send(path, method: method, body: body, idempotencyKey: idempotencyKey, timeout: timeout).0
    }

    private func send(_ path: String, method: String = "GET", body: Data? = nil, idempotencyKey: String? = nil, ifNoneMatch: String? = nil, timeout: TimeInterval = 20) async throws -> (Data, HTTPURLResponse) {
        guard let url = URL(string: path, relativeTo: baseURL) else { throw InputError.invalid("请求地址无效。") }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.httpBody = body
        request.timeoutInterval = timeout
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if body != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        if let idempotencyKey { request.setValue(idempotencyKey, forHTTPHeaderField: "Idempotency-Key") }
        if let ifNoneMatch { request.setValue(ifNoneMatch, forHTTPHeaderField: "If-None-Match") }
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw InputError.invalid("本地服务未响应。") }
        guard (200...299).contains(response.statusCode) || (response.statusCode == 304 && ifNoneMatch != nil) else {
            // Validation responses may contain submitted values, including credentials.
            // Never surface their body in the native UI or logs.
            let message: String
            switch response.statusCode {
            case 401, 403: message = "本地服务认证失败，请重新启动应用。"
            case 404: message = "任务或文件不存在。"
            case 409: message = "任务状态已变化，请刷新后重试。"
            case 410: message = "此版本文件未存档、已变化或无法通过完整性校验。"
            case 422: message = "设置不符合要求，请检查目标、授权文件夹、模型名称和 API 地址。"
            case 502: message = Self.modelFailureMessage(data)
            case 503: message = "服务暂时不可用，请检查本地服务与钥匙串权限。"
            default: message = "请求失败（\(response.statusCode)），请稍后重试。"
            }
            throw InputError.invalid(message)
        }
        return (data, response)
    }

    private func decode<T: Decodable>(_ data: Data) throws -> T {
        do { return try JSONDecoder().decode(T.self, from: data) }
        catch { throw InputError.invalid("无法读取服务返回的数据，请更新应用后重试。") }
    }

    func request<T: Decodable>(_ path: String, method: String = "GET", body: Data? = nil, idempotencyKey: String? = nil, timeout: TimeInterval = 20) async throws -> T {
        try decode(try await rawRequest(path, method: method, body: body, idempotencyKey: idempotencyKey, timeout: timeout))
    }

    /// Polls send the previous tag; an unchanged list costs a bodiless 304 and returns nil.
    func goalList(archived: Bool = false, ifNoneMatch tag: String? = nil) async throws -> GoalList? {
        let (data, response) = try await send("/goals?limit=100&archived=\(archived)", ifNoneMatch: tag)
        if response.statusCode == 304 { return nil }
        return GoalList(goals: try decode(data), tag: response.value(forHTTPHeaderField: "ETag"))
    }
    /// Searches every goal's objective, not only the loaded pages.
    func searchGoals(_ query: String, archived: Bool) async throws -> [Goal] {
        var parts = URLComponents()
        parts.path = "/goals"
        parts.queryItems = [
            URLQueryItem(name: "limit", value: "100"), URLQueryItem(name: "archived", value: "\(archived)"),
            URLQueryItem(name: "query", value: query),
        ]
        return try await request(parts.string!)
    }
    func setArchived(_ id: String, archived: Bool) async throws -> Goal {
        try await request("/goals/\(id)/\(archived ? "archive" : "restore")", method: "POST")
    }
    /// Continues a goal that failed in the execution engine from its saved progress.
    func recoverGoal(_ id: String) async throws -> Goal {
        try await request("/goals/\(id)/recover", method: "POST")
    }
    /// Continues a goal that ran out of budget or time, with these limits.
    func extendGoal(_ id: String, limits: TaskLimits) async throws -> Goal {
        try await request("/goals/\(id)/extend", method: "POST", body: JSONEncoder().encode(limits))
    }
    /// Continues an ended goal in place with the owner's follow-up request.
    func followUp(_ id: String, message: String) async throws -> Goal {
        try await request("/goals/\(id)/follow-up", method: "POST", body: JSONEncoder().encode(["message": message]))
    }
    func reviewDelivery(_ id: String, revision: Int, action: String, feedback: String) async throws -> DeliveryReviewReceipt {
        try await request("/goals/\(id)/deliveries/\(revision)/review", method: "POST", body: JSONEncoder().encode(["action": action, "feedback": feedback]))
    }
    func deliveries(_ id: String, before: Int? = nil) async throws -> DeliveryPage {
        var parts = URLComponents()
        parts.path = "/goals/\(id)/deliveries"
        parts.queryItems = [URLQueryItem(name: "limit", value: "20")]
        if let before { parts.queryItems?.append(URLQueryItem(name: "before_revision", value: String(before))) }
        return try await request(parts.string!)
    }
    func deliveryArtifact(_ id: String, revision: Int, path: String) async throws -> ArtifactContent {
        var parts = URLComponents()
        parts.path = "/goals/\(id)/deliveries/\(revision)/artifacts/content"
        parts.queryItems = [URLQueryItem(name: "path", value: path)]
        return try await request(parts.string!)
    }
    func downloadDeliveryArtifact(_ id: String, revision: Int, path: String) async throws -> Data {
        var parts = URLComponents()
        parts.path = "/goals/\(id)/deliveries/\(revision)/artifacts/download"
        parts.queryItems = [URLQueryItem(name: "path", value: path)]
        return try await rawRequest(parts.string!)
    }
    func deleteGoal(_ id: String) async throws {
        _ = try await rawRequest("/goals/\(id)", method: "DELETE")
    }
    func events(_ id: String, after: Int = 0) async throws -> [GoalEvent] {
        var all: [GoalEvent] = []
        var cursor = after
        while true {
            let page: [GoalEvent] = try await request("/goals/\(id)/events?after=\(cursor)")
            guard let last = page.last, last.event_id > cursor else { return all }
            all.append(contentsOf: page)
            cursor = last.event_id
            if page.count < 100 { return all }
        }
    }
    func messages(_ id: String) async throws -> [FeedbackMessage] { try await request("/goals/\(id)/messages") }
    func answerQuestion(_ id: String, questionID: String, answer: String) async throws {
        _ = try await rawRequest("/goals/\(id)/answer", method: "POST",
            body: JSONEncoder().encode(["question_id": questionID, "answer": answer]))
    }
    func downloadArtifact(_ id: String, path: String) async throws -> Data {
        var parts = URLComponents()
        parts.path = "/goals/\(id)/artifacts/download"
        parts.queryItems = [URLQueryItem(name: "path", value: path)]
        return try await rawRequest(parts.string!)
    }
    func providerSettings() async throws -> ProviderSettings { try await request("/provider-settings") }
    func modelQueue() async throws -> ModelQueueStatus { try await request("/model-queue") }
    func diagnostics(_ id: String) async throws -> GoalDiagnostic { try await request("/goals/\(id)/diagnostics") }
    func storageUsage(refresh: Bool = false) async throws -> StorageUsage { try await request("/storage?refresh=\(refresh)", timeout: 30) }
    func downloadDiagnostics() async throws -> Data { try await rawRequest("/diagnostics/export", timeout: 60) }
    func promptModules() async throws -> [PromptModule] { try await request("/prompt-modules") }
    func promptUses(_ id: String) async throws -> [PromptUse] { try await request("/goals/\(id)/prompts") }
    func promptSnapshot(_ id: String) async throws -> PromptComparison { try await request("/prompt-snapshots/\(id)") }
    func previewPrompt(_ purpose: String, guidance: [String]) async throws -> PromptSnapshot {
        struct Preview: Encodable { var purpose: String; var guidance: [String] }
        return try await request("/prompts/preview", method: "POST", body: JSONEncoder().encode(Preview(purpose: purpose, guidance: guidance)))
    }
    func saveProvider(_ settings: ProviderUpdate) async throws -> ProviderSettings {
        try await request("/provider-settings", method: "PUT", body: JSONEncoder().encode(settings))
    }
    func submit(_ contract: GoalContract, key: String) async throws -> Goal {
        try await request("/goals", method: "POST", body: JSONEncoder().encode(contract), idempotencyKey: key)
    }
    func prepareContract(_ draft: GoalDraft) async throws -> ContractReview {
        try await request("/goal-contract", method: "POST", body: JSONEncoder().encode(draft), timeout: 330)
    }
    /// Budget warnings for a contract the owner edited; no model call.
    func checkContract(_ contract: GoalContract) async throws -> ContractReview {
        try await request("/goal-contract/check", method: "POST", body: JSONEncoder().encode(contract))
    }
    func command(_ id: String, kind: String, message: String? = nil) async throws {
        struct Receipt: Decodable { var status: String }
        let body = try message.map { try JSONEncoder().encode(["message": $0]) }
        let _: Receipt = try await request("/goals/\(id)/\(kind)", method: "POST", body: body)
    }
    func approve(_ id: String, fingerprint: String, forTask: Bool = false) async throws {
        try await approve(id, fingerprint: fingerprint, scope: forTask ? .task : .action)
    }
    func approve(_ id: String, fingerprint: String, scope: ApprovalScope) async throws {
        struct Receipt: Decodable { var status: String }
        var payload = ["fingerprint": fingerprint]
        if scope != .action { payload["scope"] = scope.rawValue }
        let body = try JSONEncoder().encode(payload)
        let _: Receipt = try await request("/goals/\(id)/approve", method: "POST", body: body)
    }
    /// Answers several requests at once; returns how many were accepted.
    func approveBatch(_ items: [ApprovalItem], scope: ApprovalScope) async throws -> Int {
        struct Item: Encodable { var goal_id: String; var fingerprint: String; var scope: String }
        struct Batch: Encodable { var items: [Item] }
        struct Result: Decodable { var status: String }
        struct Response: Decodable { var results: [Result] }
        let body = try JSONEncoder().encode(Batch(items: items.map {
            Item(goal_id: $0.goal.id, fingerprint: $0.approval.fingerprint, scope: scope.rawValue)
        }))
        let response: Response = try await request("/approvals/batch", method: "POST", body: body)
        return response.results.filter { $0.status == "queued" }.count
    }
    func approvalPolicy() async throws -> ApprovalPolicy { try await request("/approval-policy") }
    func saveApprovalSettings(_ settings: ApprovalSettings) async throws -> ApprovalPolicy {
        try await request("/approval-policy", method: "PUT", body: JSONEncoder().encode(settings))
    }
    func addCommandRule(_ command: String) async throws {
        struct Rule: Encodable { var kind = "command"; var command: String }
        _ = try await rawRequest("/approval-rules", method: "POST", body: JSONEncoder().encode(Rule(command: command)))
    }
    func deleteApprovalRule(_ id: String) async throws {
        _ = try await rawRequest("/approval-rules/\(id)", method: "DELETE")
    }
    func memories() async throws -> [Memory] { try await request("/memories?limit=100") }
    func putMemory(key: String, kind: String, content: String) async throws -> Memory {
        try await request("/memories/\(key)", method: "PUT", body: JSONEncoder().encode(["kind": kind, "content": content]))
    }
    func deleteMemory(_ memory: Memory) async throws {
        _ = try await rawRequest("/memories/\(memory.key)?expected_revision=\(memory.revision)", method: "DELETE")
    }
    func memoryProposals() async throws -> [MemoryProposal] { try await request("/memory-proposals") }
    func resolveProposal(_ id: String, save: Bool, content: String? = nil) async throws {
        let body = try content.map { try JSONEncoder().encode(["content": $0]) }
        let path = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))) ?? id
        _ = try await rawRequest("/memory-proposals/\(path)/\(save ? "save" : "dismiss")", method: "POST", body: body)
    }
    /// Breaks a large objective into steps for review; nothing starts yet.
    func draftPlan(_ draft: GoalDraft) async throws -> PlanDraftReview {
        try await request("/plans/draft", method: "POST", body: JSONEncoder().encode(draft), timeout: 330)
    }
    func createPlan(_ plan: PlanRequest) async throws -> Plan {
        try await request("/plans", method: "POST", body: JSONEncoder().encode(plan))
    }
    func plans() async throws -> [Plan] { try await request("/plans") }
    /// pause, resume, retry, skip or cancel.
    func planAction(_ id: String, _ action: String) async throws -> Plan {
        try await request("/plans/\(id)/\(action)", method: "POST")
    }
    func deletePlan(_ id: String) async throws {
        _ = try await rawRequest("/plans/\(id)", method: "DELETE")
    }
    func schedules() async throws -> [Schedule] { try await request("/schedules") }
    func triggerRules() async throws -> [TriggerRule] { try await request("/trigger-rules") }
    func deleteTriggerRule(_ rule: TriggerRule) async throws {
        _ = try await rawRequest("/trigger-rules/\(rule.id)?revision=\(rule.revision)", method: "DELETE")
    }
    func triggerDecisions() async throws -> [TriggerDecision] { try await request("/trigger-decisions") }
    func createTriggerRule(_ rule: TriggerRuleRequest) async throws -> TriggerRule {
        try await request("/trigger-rules", method: "POST", body: JSONEncoder().encode(rule))
    }
    func setTriggerMode(_ id: String, mode: String, revision: Int) async throws -> TriggerRule {
        struct Update: Encodable { var mode: String; var revision: Int }
        return try await request("/trigger-rules/\(id)/mode", method: "POST", body: JSONEncoder().encode(Update(mode: mode, revision: revision)))
    }
    func actOnTrigger(_ id: String, action: String) async throws -> TriggerDecision {
        try await request("/trigger-decisions/\(id)/\(action)", method: "POST")
    }
    func triggerDecisionPage(pendingOnly: Bool, cursor: String? = nil) async throws -> TriggerDecisionPage {
        var parts = URLComponents()
        parts.path = "/trigger-decisions/page"
        parts.queryItems = [URLQueryItem(name: "pending_only", value: String(pendingOnly)), URLQueryItem(name: "limit", value: "25")]
        if let cursor { parts.queryItems?.append(URLQueryItem(name: "cursor", value: cursor)) }
        return try await request(parts.string!)
    }
    func setTriggerLimits(_ rule: TriggerRule, limit: Int, timezone: String) async throws -> TriggerRule {
        struct Limits: Encodable { var revision: Int; var daily_auto_limit: Int; var daily_timezone: String }
        return try await request("/trigger-rules/\(rule.id)/limits", method: "POST", body: JSONEncoder().encode(Limits(revision: rule.revision, daily_auto_limit: limit, daily_timezone: timezone)))
    }
    func createSchedule(_ schedule: ScheduleRequest) async throws -> Schedule {
        try await request("/schedules", method: "POST", body: JSONEncoder().encode(schedule))
    }
    func updateSchedule(_ id: String, _ schedule: ScheduleRequest) async throws -> Schedule {
        try await request("/schedules/\(id)", method: "PUT", body: JSONEncoder().encode(schedule))
    }
    /// pause or resume.
    func scheduleAction(_ id: String, _ action: String) async throws -> Schedule {
        try await request("/schedules/\(id)/\(action)", method: "POST")
    }
    /// Starts one run now; the regular times are unchanged.
    func runSchedule(_ id: String) async throws -> Goal {
        try await request("/schedules/\(id)/run", method: "POST")
    }
    func deleteSchedule(_ id: String) async throws {
        _ = try await rawRequest("/schedules/\(id)", method: "DELETE")
    }
    func artifact(_ id: String, path: String) async throws -> ArtifactContent {
        var parts = URLComponents()
        parts.path = "/goals/\(id)/artifacts/content"
        parts.queryItems = [URLQueryItem(name: "path", value: path)]
        return try await request(parts.string!)
    }
}
