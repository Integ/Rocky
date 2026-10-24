import Foundation

struct APIClient {
    let baseURL: URL
    let token: String
    var session: URLSession = .shared

    private func rawRequest(_ path: String, method: String = "GET", body: Data? = nil, idempotencyKey: String? = nil, timeout: TimeInterval = 20) async throws -> Data {
        guard let url = URL(string: path, relativeTo: baseURL) else { throw InputError.invalid("请求地址无效。") }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.httpBody = body
        request.timeoutInterval = timeout
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if body != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        if let idempotencyKey { request.setValue(idempotencyKey, forHTTPHeaderField: "Idempotency-Key") }
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw InputError.invalid("本地服务未响应。") }
        guard (200...299).contains(response.statusCode) else {
            // Validation responses may contain submitted values, including credentials.
            // Never surface their body in the native UI or logs.
            let message: String
            switch response.statusCode {
            case 401, 403: message = "本地服务认证失败，请重新启动应用。"
            case 404: message = "任务或文件不存在。"
            case 409: message = "任务状态已变化，请刷新后重试。"
            case 422: message = "设置不符合要求，请检查目标、模型名称和 API 地址。"
            case 502: message = "AI 未能完成任务评估，请检查模型配置后重新提交。"
            case 503: message = "服务暂时不可用，请检查本地服务与钥匙串权限。"
            default: message = "请求失败（\(response.statusCode)），请稍后重试。"
            }
            throw InputError.invalid(message)
        }
        return data
    }

    func request<T: Decodable>(_ path: String, method: String = "GET", body: Data? = nil, idempotencyKey: String? = nil, timeout: TimeInterval = 20) async throws -> T {
        let data = try await rawRequest(path, method: method, body: body, idempotencyKey: idempotencyKey, timeout: timeout)
        do { return try JSONDecoder().decode(T.self, from: data) }
        catch { throw InputError.invalid("无法读取服务返回的数据，请更新应用后重试。") }
    }

    func goals(archived: Bool = false) async throws -> [Goal] {
        try await request("/goals?limit=100&archived=\(archived)")
    }
    func setArchived(_ id: String, archived: Bool) async throws -> Goal {
        try await request("/goals/\(id)/\(archived ? "archive" : "restore")", method: "POST")
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
    func prepareContract(_ draft: GoalDraft) async throws -> GoalContract {
        try await request("/goal-contract", method: "POST", body: JSONEncoder().encode(draft), timeout: 330)
    }
    func command(_ id: String, kind: String, message: String? = nil) async throws {
        struct Receipt: Decodable { var status: String }
        let body = try message.map { try JSONEncoder().encode(["message": $0]) }
        let _: Receipt = try await request("/goals/\(id)/\(kind)", method: "POST", body: body)
    }
    func approve(_ id: String, fingerprint: String, forTask: Bool = false) async throws {
        struct Receipt: Decodable { var status: String }
        var payload = ["fingerprint": fingerprint]
        if forTask { payload["scope"] = "task" }
        let body = try JSONEncoder().encode(payload)
        let _: Receipt = try await request("/goals/\(id)/approve", method: "POST", body: body)
    }
    func artifact(_ id: String, path: String) async throws -> ArtifactContent {
        var parts = URLComponents()
        parts.path = "/goals/\(id)/artifacts/content"
        parts.queryItems = [URLQueryItem(name: "path", value: path)]
        return try await request(parts.string!)
    }
}
