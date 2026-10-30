import Foundation

struct APIClient {
    let baseURL: URL
    let token: String
    var session: URLSession = .shared

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
            case 422: message = "设置不符合要求，请检查目标、授权文件夹、模型名称和 API 地址。"
            case 502: message = "AI 未能完成任务评估，请检查模型配置后重新提交。"
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
    func downloadArtifact(_ id: String, path: String) async throws -> Data {
        var parts = URLComponents()
        parts.path = "/goals/\(id)/artifacts/download"
        parts.queryItems = [URLQueryItem(name: "path", value: path)]
        return try await rawRequest(parts.string!)
    }
    func providerSettings() async throws -> ProviderSettings { try await request("/provider-settings") }
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
    func artifact(_ id: String, path: String) async throws -> ArtifactContent {
        var parts = URLComponents()
        parts.path = "/goals/\(id)/artifacts/content"
        parts.queryItems = [URLQueryItem(name: "path", value: path)]
        return try await request(parts.string!)
    }
}
