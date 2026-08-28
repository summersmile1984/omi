CREATE VIRTUAL TABLE IF NOT EXISTS cf_conversations_fts USING fts5(
  uid UNINDEXED,
  uid_token,
  conversation_id UNINDEXED,
  searchable_text,
  tokenize = 'unicode61 remove_diacritics 2'
);

INSERT INTO cf_conversations_fts (rowid, uid, uid_token, conversation_id, searchable_text)
SELECT
  rowid,
  uid,
  lower(hex(uid)),
  id,
  id || ' ' ||
  COALESCE(json_extract(CASE WHEN json_valid(structured_json) THEN structured_json ELSE '{}' END, '$.title'), '') || ' ' ||
  COALESCE(json_extract(CASE WHEN json_valid(structured_json) THEN structured_json ELSE '{}' END, '$.overview'), '') || ' ' ||
  COALESCE(json_extract(CASE WHEN json_valid(structured_json) THEN structured_json ELSE '{}' END, '$.category'), '') || ' ' ||
  COALESCE(
    (
      SELECT group_concat(COALESCE(json_extract(segment.value, '$.text'), ''), ' ')
      FROM json_each(
        CASE WHEN json_valid(cf_conversations.transcript_segments_json)
          THEN cf_conversations.transcript_segments_json
          ELSE '[]'
        END
      ) AS segment
    ),
    ''
  )
FROM cf_conversations;

CREATE TRIGGER IF NOT EXISTS cf_conversations_fts_insert
AFTER INSERT ON cf_conversations
BEGIN
  INSERT INTO cf_conversations_fts (rowid, uid, uid_token, conversation_id, searchable_text)
  VALUES (
    new.rowid,
    new.uid,
    lower(hex(new.uid)),
    new.id,
    new.id || ' ' ||
    COALESCE(json_extract(CASE WHEN json_valid(new.structured_json) THEN new.structured_json ELSE '{}' END, '$.title'), '') || ' ' ||
    COALESCE(json_extract(CASE WHEN json_valid(new.structured_json) THEN new.structured_json ELSE '{}' END, '$.overview'), '') || ' ' ||
    COALESCE(json_extract(CASE WHEN json_valid(new.structured_json) THEN new.structured_json ELSE '{}' END, '$.category'), '') || ' ' ||
    COALESCE(
      (
        SELECT group_concat(COALESCE(json_extract(segment.value, '$.text'), ''), ' ')
        FROM json_each(
          CASE WHEN json_valid(new.transcript_segments_json)
            THEN new.transcript_segments_json
            ELSE '[]'
          END
        ) AS segment
      ),
      ''
    )
  );
END;

CREATE TRIGGER IF NOT EXISTS cf_conversations_fts_update
AFTER UPDATE OF structured_json, transcript_segments_json ON cf_conversations
BEGIN
  DELETE FROM cf_conversations_fts WHERE rowid = old.rowid;
  INSERT INTO cf_conversations_fts (rowid, uid, uid_token, conversation_id, searchable_text)
  VALUES (
    new.rowid,
    new.uid,
    lower(hex(new.uid)),
    new.id,
    new.id || ' ' ||
    COALESCE(json_extract(CASE WHEN json_valid(new.structured_json) THEN new.structured_json ELSE '{}' END, '$.title'), '') || ' ' ||
    COALESCE(json_extract(CASE WHEN json_valid(new.structured_json) THEN new.structured_json ELSE '{}' END, '$.overview'), '') || ' ' ||
    COALESCE(json_extract(CASE WHEN json_valid(new.structured_json) THEN new.structured_json ELSE '{}' END, '$.category'), '') || ' ' ||
    COALESCE(
      (
        SELECT group_concat(COALESCE(json_extract(segment.value, '$.text'), ''), ' ')
        FROM json_each(
          CASE WHEN json_valid(new.transcript_segments_json)
            THEN new.transcript_segments_json
            ELSE '[]'
          END
        ) AS segment
      ),
      ''
    )
  );
END;

CREATE TRIGGER IF NOT EXISTS cf_conversations_fts_delete
AFTER DELETE ON cf_conversations
BEGIN
  DELETE FROM cf_conversations_fts WHERE rowid = old.rowid;
END;
