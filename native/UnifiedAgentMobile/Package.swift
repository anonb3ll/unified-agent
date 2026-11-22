// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "UnifiedAgentMobile",
    platforms: [.iOS(.v17), .watchOS(.v10), .macOS(.v14)],
    products: [.library(name: "UnifiedAgentMobile", targets: ["UnifiedAgentMobile"])],
    targets: [
        .target(name: "UnifiedAgentMobile"),
        .testTarget(name: "UnifiedAgentMobileTests", dependencies: ["UnifiedAgentMobile"]),
    ]
)
