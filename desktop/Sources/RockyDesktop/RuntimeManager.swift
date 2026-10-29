import AppKit
import Combine
import Darwin
import Foundation
import ServiceManagement

/// Connects to the Rocky background service, a launchd agent the app registers
/// through SMAppService. The agent (`rocky serve`) owns Temporal, the API and the
/// worker and keeps running after the app quits; the app only observes it.
@MainActor
final class RuntimeManager: ObservableObject {
    enum Phase: String { case stopped, preparing, starting, ready, failed }

    static let agentLabel = "dev.rocky.agent"
    static let agentPlist = "dev.rocky.agent.plist"

    @Published private(set) var phase: Phase = .stopped
    @Published private(set) var status = "后台服务尚未连接"
    @Published private(set) var detail = ""
    /// Persistent owner token created by the service on first start.
    @Published private(set) var apiToken = ""
    /// True after the owner stopped the service from the app.
    @Published private(set) var serviceStopped = false
    let apiURL = URL(string: "http://127.0.0.1:18741")!
    let applicationSupportURL: URL
    var workspaceURL: URL { applicationSupportURL.appendingPathComponent("data/workspaces") }
    var logsURL: URL { applicationSupportURL.appendingPathComponent("logs") }
    var runURL: URL { applicationSupportURL.appendingPathComponent("run") }
    var tokenURL: URL { applicationSupportURL.appendingPathComponent("data/api-token") }
    var isReady: Bool { phase == .ready }

    private var monitor: Task<Void, Never>?
    private var generation = UUID()
    private let service = SMAppService.agent(plistName: RuntimeManager.agentPlist)
    private let fm = FileManager.default

    init() {
        applicationSupportURL = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Rocky", isDirectory: true)
    }

    func start() async {
        guard phase == .stopped || phase == .failed else { return }
        let run = UUID()
        generation = run
        serviceStopped = false
        monitor?.cancel()
        monitor = nil
        phase = .preparing
        status = "正在连接后台服务"
        detail = "Rocky 在后台常驻运行，关闭窗口后任务会继续。首次启动会下载 Python 和依赖，请保持网络连接。"
        do {
            try registerAgent()
            try await waitUntilReady(run: run, timeout: 900)
            guard generation == run else { return }
            markReady()
            startMonitoring(run: run)
        } catch is CancellationError {
            if generation == run { phase = .stopped }
        } catch {
            if generation == run { fail(error) }
        }
    }

    /// Stops observing the service. The service and its tasks keep running.
    func disconnect() {
        generation = UUID()
        monitor?.cancel()
        monitor = nil
        phase = .stopped
        status = "后台服务尚未连接"
    }

    /// Restarts the agent; unfinished tasks resume from their durable history.
    func restartService() async {
        disconnect()
        do {
            try registerAgent()
            try kickstart()
        } catch {
            fail(error)
            return
        }
        await start()
    }

    /// Unregisters the agent so launchd stops it. Starting again registers it.
    func stopService() async {
        disconnect()
        do {
            try await service.unregister()
            serviceStopped = true
            phase = .failed
            status = "后台服务已停止"
            detail = "未结束的任务会在后台服务再次启动后继续。点击下方按钮或重新打开 Rocky 即可启动。"
        } catch {
            fail(error)
        }
    }

    private func markReady() {
        phase = .ready
        status = "本地引擎已就绪"
        detail = "任务、记录和产物保存在这台 Mac 上。关闭应用后任务继续在后台运行。"
    }

    private func fail(_ error: Error) {
        phase = .failed
        status = "无法连接后台服务"
        detail = (error as? RuntimeFailure)?.message ?? error.localizedDescription
    }

    private func registerAgent() throws {
        guard Bundle.main.bundleURL.pathExtension == "app" else {
            throw RuntimeFailure("后台服务需要完整的应用包。请使用 scripts/build_macos.sh 构建 Rocky.app 后打开。")
        }
        if service.status != .enabled {
            do {
                try service.register()
            } catch {
                if service.status != .requiresApproval {
                    throw RuntimeFailure("无法注册后台服务：\(error.localizedDescription)")
                }
            }
        }
        if service.status == .requiresApproval {
            SMAppService.openSystemSettingsLoginItems()
            throw RuntimeFailure("请在“系统设置 > 通用 > 登录项与扩展”中允许 Rocky 在后台运行，然后点击重试。")
        }
    }

    private func waitUntilReady(run: UUID, timeout: TimeInterval) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        let expectedBuild = bundleBuild()
        var restarted = false
        while Date() < deadline {
            guard generation == run, !Task.isCancelled else { throw CancellationError() }
            if let token = readToken() { apiToken = token }
            let state = readStatus()
            if state.phase == "ready", !apiToken.isEmpty, await apiIsHealthy() {
                // A service left running by an older build is restarted once so
                // it stages the backend shipped with this app.
                if expectedBuild == nil || readText(runURL.appendingPathComponent("backend-build")) == expectedBuild { return }
                if !restarted {
                    try kickstart()
                    restarted = true
                }
            }
            switch state.phase {
            case "preparing":
                phase = .preparing
                status = "正在准备本地运行环境"
            case "starting":
                phase = .starting
                status = "正在启动执行引擎"
            case "failed":
                status = "后台服务启动失败，正在自动重试"
                if let message = state.detail, !message.isEmpty { detail = message }
            default:
                break
            }
            if service.status == .requiresApproval {
                throw RuntimeFailure("请在“系统设置 > 通用 > 登录项与扩展”中允许 Rocky 在后台运行，然后点击重试。")
            }
            try await Task.sleep(for: .milliseconds(500))
        }
        throw RuntimeFailure("后台服务启动超时。可打开日志查看原因，再重试。")
    }

    private func kickstart() throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = ["kickstart", "-k", "gui/\(getuid())/\(Self.agentLabel)"]
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            throw RuntimeFailure("无法重启后台服务（launchctl 返回 \(process.terminationStatus)）。")
        }
    }

    private struct AgentStatus: Decodable {
        var phase: String
        var detail: String?
    }

    private func readStatus() -> AgentStatus {
        guard let data = try? Data(contentsOf: runURL.appendingPathComponent("status")),
              let value = try? JSONDecoder().decode(AgentStatus.self, from: data) else {
            return AgentStatus(phase: "unknown", detail: nil)
        }
        return value
    }

    private func readText(_ url: URL) -> String? {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return nil }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private func readToken() -> String? {
        guard let token = readText(tokenURL), token.count >= 32 else { return nil }
        return token
    }

    private func bundleBuild() -> String? {
        Bundle.main.resourceURL.flatMap { readText($0.appendingPathComponent("backend/BUILD_ID")) }
    }

    private func apiIsHealthy() async -> Bool {
        var request = URLRequest(url: apiURL.appendingPathComponent("health"), timeoutInterval: 2)
        request.setValue("Bearer \(apiToken)", forHTTPHeaderField: "Authorization")
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200,
                  let json = try JSONSerialization.jsonObject(with: data) as? [String: String] else { return false }
            return json["status"] == "ok" && json["service"] == "rocky"
        } catch { return false }
    }

    /// launchd restarts a crashed service; the client waits and reconnects
    /// instead of stopping anything.
    private func startMonitoring(run: UUID) {
        monitor = Task { [weak self] in
            var failures = 0
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                guard let self, self.generation == run else { return }
                if self.service.status != .enabled {
                    self.monitor = nil
                    self.phase = .failed
                    self.status = "后台服务已关闭"
                    self.detail = "Rocky 的后台运行权限已被关闭。点击下方按钮重新启动，或在“系统设置 > 通用 > 登录项与扩展”中允许。"
                    return
                }
                if let token = self.readToken() { self.apiToken = token }
                if await self.apiIsHealthy() {
                    failures = 0
                    if self.phase != .ready { self.markReady() }
                } else {
                    failures += 1
                    if failures >= 2, self.phase == .ready {
                        self.phase = .starting
                        self.status = "正在重新连接后台服务"
                        self.detail = "后台服务会自动重启，任务记录已保存。"
                    }
                }
            }
        }
    }
}

private struct RuntimeFailure: Error { let message: String; init(_ message: String) { self.message = message } }
