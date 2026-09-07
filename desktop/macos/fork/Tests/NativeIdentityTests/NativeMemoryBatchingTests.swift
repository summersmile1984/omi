import Foundation
import NativeIdentity
import Testing

private struct Item: Encodable, Equatable {
  let content: String
  var tags: [String] = []
}
private struct Body: Encodable { let memories: [Item] }

@Test(arguments: ["x", "汉", "😀", "\"/\\\n"])
func memoryBatchesMeasureActualEncodedBytes(character: String) throws {
  let items = (0..<125).map {
    Item(
      content: String(repeating: character, count: 20_000) + String($0),
      tags: ["import", character])
  }
  let encoder = JSONEncoder()
  let plan = try NativeMemoryBatching.plan(
    items, maxCount: 100, maxBytes: 1_000_000, encoder: encoder)
  #expect(plan.rejectedCount == 0)
  #expect(plan.batches.flatMap { $0 } == items)
  #expect(plan.batches.count > 1)
  for batch in plan.batches {
    #expect(batch.count <= 100)
    #expect(try encoder.encode(Body(memories: batch)).count <= 1_000_000)
  }
}

@Test
func exactBoundaryIncludesEnvelopeMetadataAndEscapes() throws {
  let items = [Item(content: "茶😀\"/", tags: ["metadata"]), Item(content: "next")]
  let encoder = JSONEncoder()
  // A different valid encoder format must also be measured, not estimated.
  encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
  let exact = try encoder.encode(Body(memories: items)).count
  let fits = try NativeMemoryBatching.plan(items, maxCount: 100, maxBytes: exact, encoder: encoder)
  #expect(fits.batches == [items])
  let split = try NativeMemoryBatching.plan(
    items, maxCount: 100, maxBytes: exact - 1, encoder: encoder)
  #expect(split.batches == items.map { [$0] })
}

@Test
func individuallyOversizeItemIsCountedWithoutDroppingNeighbors() throws {
  let first = Item(content: "first")
  let last = Item(content: "last")
  let oversized = Item(
    content: "short content", tags: [String(repeating: "large metadata", count: 100_000)])
  let plan = try NativeMemoryBatching.plan(
    [first, oversized, last], maxCount: 100, maxBytes: 1_000_000, encoder: JSONEncoder())
  #expect(plan.rejectedCount == 1)
  #expect(plan.batches.flatMap { $0 } == [first, last])
}

@Test
func serverAndSmallCloudflareImportsKeepHundredItemBatches() throws {
  let items = (0..<250).map { Item(content: "Memory \($0)") }
  for bytes: Int? in [nil, 1_000_000] {
    let plan = try NativeMemoryBatching.plan(
      items, maxCount: 100, maxBytes: bytes, encoder: JSONEncoder())
    #expect(plan.batches.map(\.count) == [100, 100, 50])
    #expect(plan.batches.flatMap { $0 } == items)
    #expect(plan.rejectedCount == 0)
  }
}
