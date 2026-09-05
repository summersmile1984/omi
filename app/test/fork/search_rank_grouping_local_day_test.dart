import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/providers/conversation_provider.dart';

ServerConversation _conversation(String id, DateTime startedAt) {
  return ServerConversation(
    id: id,
    createdAt: startedAt,
    startedAt: startedAt,
    structured: Structured(id, 'overview'),
  );
}

void main() {
  test('local-day search hits preserve server rank in staged apps', () {
    final day = DateTime(2026, 8, 12);
    final first = _conversation('rank-1', day.add(const Duration(hours: 1)));
    final second = _conversation('rank-2', day.add(const Duration(hours: 23)));

    final grouped = groupSearchResultsPreservingRank([first, second]);

    expect(conversationLocalDayKey(first.startedAt!), conversationLocalDayKey(second.startedAt!));
    expect(grouped.values.single.map((conversation) => conversation.id), ['rank-1', 'rank-2']);
  });
}
