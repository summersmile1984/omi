import Foundation

public struct NativeMemoryBatchPlan<Item> {
  public let batches: [[Item]]
  public let rejectedCount: Int
}

private struct NativeMemoryBatchBody<Item: Encodable>: Encodable {
  let memories: [Item]
}

/// Plans complete request bodies before any import writes. Each emitted batch
/// remains one request, so the caller retains its per-batch outcome and retry.
public enum NativeMemoryBatching {
  public static let cloudflareMaxBytes = 1_000_000

  public static func plan<Item: Encodable>(
    _ items: [Item], maxCount: Int, maxBytes: Int?, encoder: JSONEncoder
  ) throws -> NativeMemoryBatchPlan<Item> {
    precondition(maxCount > 0)
    precondition(maxBytes == nil || maxBytes! > 0)
    var batches: [[Item]] = []
    var rejected = 0
    var start = 0
    while start < items.count {
      let limit = start + min(maxCount, items.count - start)
      var end = limit
      if let maxBytes {
        func fits(_ candidateEnd: Int) throws -> Bool {
          try encoder.encode(NativeMemoryBatchBody(memories: Array(items[start..<candidateEnd])))
            .count <= maxBytes
        }
        if try !fits(limit) {
          var low = start
          var high = limit - 1
          while low < high {
            let middle = low + (high - low + 1) / 2
            if try fits(middle) { low = middle } else { high = middle - 1 }
          }
          end = low
        }
      }
      if end == start {
        rejected += 1
        start += 1
      } else {
        batches.append(Array(items[start..<end]))
        start = end
      }
    }
    return NativeMemoryBatchPlan(batches: batches, rejectedCount: rejected)
  }
}
