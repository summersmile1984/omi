// swift-tools-version: 6.0
import PackageDescription

let package = Package(
  name: "ForkNativeIdentity",
  platforms: [.macOS("14.0")],
  products: [.library(name: "NativeIdentity", targets: ["NativeIdentity"])],
  targets: [
    .target(name: "NativeIdentity"),
    .testTarget(name: "NativeIdentityTests", dependencies: ["NativeIdentity"]),
  ],
  swiftLanguageModes: [.v6]
)
