import AppKit

enum ForkNativeBrand {
  enum AssetError: Error { case missing, invalidImage }

  static func image(dark: Bool, in bundle: Bundle) throws -> NSImage {
    let name = dark ? "ForkBrandDark" : "ForkBrandLight"
    guard let url = bundle.url(forResource: name, withExtension: "png") else { throw AssetError.missing }
    guard let image = NSImage(contentsOf: url), image.isValid, image.size.width > 0,
      image.size.height > 0
    else { throw AssetError.invalidImage }
    return image
  }
}
