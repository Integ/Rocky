import AppKit
import Combine
import Darwin
import Foundation

/// Owns the three local services. Never discovers, adopts, or stops unrelated services.
@MainActor
final class RuntimeManager: ObservableObject {
    enum Phase: String { case stopped, preparing, starting, ready, failed }

    @Published private(set) var phase: Phase = .stopped
    @Published private(set) var status = "本地服务尚未启动"
    @Published private(set) var detail = ""
    let apiURL = URL(string: "http://127.0.0.1:18741")!
    let apiToken = UUID().uuidString + UUID().uuidString
    let applicationSupportURL: URL
    var workspaceURL: URL { applicationSupportURL.appendingPathComponent("data/workspaces") }
    var logsURL: URL { applicationSupportURL.appendingPathComponent("logs") }
    var isReady: Bool { phase == .ready }

    private var services: [Process] = []
    private var setupProcess: Process?
    private var monitor: Task<Void, Never>?
    private var generation = UUID()
    private var lockFD: Int32 = -1
    private var readyFile: URL?
    private var logHandles: [FileHandle] = []
    private let fm = FileManager.default

    init() {
        applicationSupportURL = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Rocky", isDirectory: true)
    }

    func start() async {
        guard phase == .stopped || phase == .failed else { return }
        stop()
        let run = UUID()
        generation = run
        phase = .preparing
        status = "正在准备本地运行环境"
        detail = "首次启动会下载 Python 和依赖，请保持网络连接。以后可直接打开应用。"
        do {
            try prepareDirectoriesAndLock()
            try checkPorts()
            guard let resources = Bundle.main.resourceURL else { throw RuntimeFailure("找不到应用资源。请重新构建 Rocky.app。") }
            let bundled = resources.appendingPathComponent("backend")
            let uv = resources.appendingPathComponent("runtime/bin/uv")
            let temporal = resources.appendingPathComponent("runtime/bin/temporal")
            guard fm.isExecutableFile(atPath: uv.path), fm.isExecutableFile(atPath: temporal.path),
                  fm.fileExists(atPath: bundled.appendingPathComponent("pyproject.toml").path) else {
                throw RuntimeFailure("运行组件不完整。请使用 scripts/build_macos.sh 构建完整应用。")
            }
            let project = applicationSupportURL.appendingPathComponent("backend")
            try stageBackend(from: bundled, to: project)
            var environment = serviceEnvironment(project: project)
            let marker = applicationSupportURL.appendingPathComponent("worker-ready-\(run.uuidString)")
            readyFile = marker
            environment["ROCKY_WORKER_READY_FILE"] = marker.path
            let setup = try launch(uv, ["sync", "--frozen", "--no-dev", "--no-editable", "--python", "3.12", "--managed-python", "--reinstall-package", "rocky-agent"],
                                   project: project, environment: environment, log: "setup")
            setupProcess = setup
            while setup.isRunning {
                try await Task.sleep(for: .milliseconds(150))
                guard generation == run, !Task.isCancelled else { throw CancellationError() }
            }
            setupProcess = nil
            guard setup.terminationStatus == 0 else {
                throw RuntimeFailure("Python 环境准备失败。检查网络后重试；详细信息见本地 setup.log。")
            }
            guard generation == run, !Task.isCancelled else { throw CancellationError() }
            phase = .starting
            status = "正在启动执行引擎"
            detail = "正在连接本地任务存储和工作进程。"
            let temporalProcess = try launch(temporal, ["server", "start-dev", "--ip", "127.0.0.1", "--port", "18743",
                "--headless", "--db-filename", applicationSupportURL.appendingPathComponent("data/temporal.db").path],
                project: project, environment: environment, log: "temporal")
            services.append(temporalProcess)
            let python = project.appendingPathComponent(".venv/bin/python")
            try await waitForTemporal(python: python, project: project, environment: environment, run: run)
            let api = try launch(python, ["-m", "rocky.cli", "api"], project: project, environment: environment, log: "api")
            services.append(api)
            let worker = try launch(python, ["-m", "rocky.cli", "worker"], project: project, environment: environment, log: "worker")
            services.append(worker)
            let deadline = Date().addingTimeInterval(60)
            while Date() < deadline {
                guard generation == run, !Task.isCancelled else { throw CancellationError() }
                guard services.allSatisfy(\.isRunning) else {
                    throw RuntimeFailure("本地执行服务意外退出。可打开日志查看原因，再重试启动。")
                }
                if fm.fileExists(atPath: marker.path), await apiIsHealthy() {
                    phase = .ready
                    status = "本地引擎已就绪"
                    detail = "任务、记录和产物保存在这台 Mac 上。"
                    startMonitoring(run: run)
                    return
                }
                try await Task.sleep(for: .milliseconds(250))
            }
            throw RuntimeFailure("执行引擎启动超时。请检查本地日志后重试。")
        } catch is CancellationError {
            if generation == run { stop() }
        } catch {
            if generation == run {
                stop()
                phase = .failed
                status = "本地引擎启动失败"
                detail = (error as? RuntimeFailure)?.message ?? "无法准备本地运行环境。请检查日志后重试。"
            }
        }
    }

    /// Synchronous cleanup is intentional: application termination must not orphan child services.
    func stop() {
        generation = UUID()
        monitor?.cancel()
        monitor = nil
        let owned = ([setupProcess].compactMap { $0 } + services).reversed()
        for process in owned where process.isRunning { process.terminate() }
        let deadline = Date().addingTimeInterval(2)
        while owned.contains(where: \.isRunning), Date() < deadline { Thread.sleep(forTimeInterval: 0.025) }
        for process in owned where process.isRunning { kill(process.processIdentifier, SIGKILL) }
        for process in owned { process.waitUntilExit() }
        setupProcess = nil
        services.removeAll()
        logHandles.forEach { try? $0.close() }
        logHandles.removeAll()
        if let readyFile { try? fm.removeItem(at: readyFile) }
        readyFile = nil
        if lockFD >= 0 { flock(lockFD, LOCK_UN); close(lockFD); lockFD = -1 }
        phase = .stopped
        status = "本地引擎已停止"
    }

    private func prepareDirectoriesAndLock() throws {
        for path in [applicationSupportURL, applicationSupportURL.appendingPathComponent("data"), workspaceURL, logsURL,
                     applicationSupportURL.appendingPathComponent("cache"), applicationSupportURL.appendingPathComponent("cache/uv"),
                     applicationSupportURL.appendingPathComponent("runtime"), applicationSupportURL.appendingPathComponent("runtime/python")] {
            try ensureDirectory(path)
        }
        let lockURL = applicationSupportURL.appendingPathComponent("desktop.lock")
        lockFD = open(lockURL.path, O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, S_IRUSR | S_IWUSR)
        guard lockFD >= 0 else { throw RuntimeFailure("无法锁定本地数据目录。") }
        guard flock(lockFD, LOCK_EX | LOCK_NB) == 0 else {
            close(lockFD); lockFD = -1
            throw RuntimeFailure("Rocky 已在运行。请切换到已打开的应用，或退出另一个实例后重试。")
        }
    }

    private func stageBackend(from source: URL, to destination: URL) throws {
        try ensureDirectory(destination)
        try rejectSymbolicLink(destination.appendingPathComponent(".venv"))
        for name in ["src", "pyproject.toml", "uv.lock", "README.md"] {
            let target = destination.appendingPathComponent(name)
            if fm.fileExists(atPath: target.path) { try fm.removeItem(at: target) }
            try fm.copyItem(at: source.appendingPathComponent(name), to: target)
        }
    }

    private func rejectSymbolicLink(_ path: URL) throws {
        // Compare the actual file type, not canonical path strings: APFS may
        // normalize case ("Rocky" -> "rocky") without any symbolic link.
        if let attributes = try? fm.attributesOfItem(atPath: path.path),
           attributes[.type] as? FileAttributeType == .typeSymbolicLink {
            throw RuntimeFailure("运行目录包含符号链接，请使用普通的 Rocky 应用数据目录。")
        }
    }

    private func ensureDirectory(_ path: URL) throws {
        try rejectSymbolicLink(path)
        try fm.createDirectory(at: path, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let values = try path.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
        guard values.isDirectory == true, values.isSymbolicLink != true else {
            throw RuntimeFailure("本地运行环境需要普通目录。")
        }
    }

    private func serviceEnvironment(project: URL) -> [String: String] {
        // Ignore arbitrary inherited ROCKY settings, provider keys, proxies and Python paths.
        let runtimeBin = Bundle.main.resourceURL?.appendingPathComponent("runtime/bin").path ?? ""
        var env: [String: String] = [
            "PATH": project.appendingPathComponent(".venv/bin").path + ":" + runtimeBin + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": fm.homeDirectoryForCurrentUser.path,
            "TMPDIR": NSTemporaryDirectory(), "LANG": "en_US.UTF-8", "PYTHONUNBUFFERED": "1",
            "UV_CACHE_DIR": applicationSupportURL.appendingPathComponent("cache/uv").path,
            "UV_PYTHON_INSTALL_DIR": applicationSupportURL.appendingPathComponent("runtime/python").path,
            "UV_PROJECT_ENVIRONMENT": project.appendingPathComponent(".venv").path,
            "UV_NO_CONFIG": "1", "UV_NO_ENV_FILE": "1", "UV_NATIVE_TLS": "true",
            "ROCKY_AUTH_MODE": "token", "ROCKY_API_TOKEN": apiToken,
            "ROCKY_HOST": "127.0.0.1", "ROCKY_PORT": "18741",
            "ROCKY_TEMPORAL_ADDRESS": "127.0.0.1:18743", "ROCKY_TASK_QUEUE": "rocky-desktop",
            "ROCKY_DATABASE_URL": "sqlite:///" + applicationSupportURL.appendingPathComponent("data/rocky.db").path,
            "ROCKY_WORKSPACE_ROOT": workspaceURL.path,
            "ROCKY_TOOL_ACCESS": "full",
            "ROCKY_CREDENTIAL_PATH": applicationSupportURL.appendingPathComponent("data/credentials.json").path,
        ]
        // Certificate configuration supports managed corporate networks without importing credentials.
        for key in ["SSL_CERT_FILE", "SSL_CERT_DIR"] { if let value = ProcessInfo.processInfo.environment[key] { env[key] = value } }
        return env
    }

    private func launch(_ executable: URL, _ arguments: [String], project: URL,
                        environment: [String: String], log name: String) throws -> Process {
        let logURL = logsURL.appendingPathComponent("\(name).log")
        let fd = open(logURL.path, O_CREAT | O_WRONLY | O_TRUNC | O_NOFOLLOW | O_CLOEXEC, S_IRUSR | S_IWUSR)
        guard fd >= 0 else { throw RuntimeFailure("无法打开本地日志文件。") }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        logHandles.append(handle)
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.currentDirectoryURL = project
        process.environment = environment
        process.standardOutput = handle
        process.standardError = handle
        process.standardInput = FileHandle.nullDevice
        try process.run()
        return process
    }

    private func waitForTemporal(python: URL, project: URL, environment: [String: String], run: UUID) async throws {
        let probe = "import asyncio, os\nfrom temporalio.client import Client\nasync def main():\n for _ in range(200):\n  try:\n   await Client.connect(os.environ['ROCKY_TEMPORAL_ADDRESS'])\n   return\n  except Exception:\n   await asyncio.sleep(.2)\n raise SystemExit(1)\nasyncio.run(main())"
        let process = try launch(python, ["-c", probe], project: project, environment: environment, log: "startup")
        setupProcess = process
        while process.isRunning {
            guard generation == run, !Task.isCancelled else { throw CancellationError() }
            guard services.allSatisfy(\.isRunning) else { throw RuntimeFailure("任务存储服务无法启动，请检查 temporal.log。") }
            try await Task.sleep(for: .milliseconds(200))
        }
        setupProcess = nil
        guard process.terminationStatus == 0 else { throw RuntimeFailure("无法连接本地任务存储。请检查 temporal.log 后重试。") }
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

    private func startMonitoring(run: UUID) {
        monitor = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                guard let self, self.generation == run else { return }
                let healthy = await self.apiIsHealthy()
                if !self.services.allSatisfy(\.isRunning) || !healthy {
                    self.stop()
                    self.phase = .failed
                    self.status = "本地引擎连接中断"
                    self.detail = "任务记录已保存。请重试启动，以恢复未完成的任务。"
                    return
                }
            }
        }
    }

    private func checkPorts() throws {
        for port: UInt16 in [18741, 18743] {
            let fd = socket(AF_INET, SOCK_STREAM, 0)
            guard fd >= 0 else { throw RuntimeFailure("无法创建本地网络连接。") }
            defer { close(fd) }
            var reuse: Int32 = 1
            setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout<Int32>.size))
            var address = sockaddr_in()
            address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
            address.sin_family = sa_family_t(AF_INET)
            address.sin_port = port.bigEndian
            address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
            let result = withUnsafePointer(to: &address) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
            }
            guard result == 0 else { throw RuntimeFailure("本地端口 \(port) 已被占用。请关闭占用该端口的服务后重试。") }
        }
    }
}

private struct RuntimeFailure: Error { let message: String; init(_ message: String) { self.message = message } }
