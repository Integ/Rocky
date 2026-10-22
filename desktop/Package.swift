// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "RockyDesktop",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "RockyDesktop", targets: ["RockyDesktop"])],
    targets: [
        .executableTarget(name: "RockyDesktop"),
        .testTarget(name: "RockyDesktopTests", dependencies: ["RockyDesktop"]),
    ]
)
