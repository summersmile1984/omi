-- A durable multipart handle precedes every image byte. Jobs aborts the handle
-- before deleting a cleanup object, preventing a late complete after erasure.
ALTER TABLE cf_conversations ADD COLUMN has_content INTEGER NOT NULL DEFAULT 0 CHECK (has_content IN (0,1));
ALTER TABLE cf_conversations ADD COLUMN has_photos INTEGER NOT NULL DEFAULT 0 CHECK (has_photos IN (0,1));

CREATE TABLE cf_frame_objects (
  object_id TEXT PRIMARY KEY NOT NULL,
  uid TEXT NOT NULL,
  request_id TEXT NOT NULL,
  storage_id TEXT NOT NULL,
  tier TEXT NOT NULL CHECK (tier IN ('temporary','permanent')),
  account_generation INTEGER NOT NULL,
  authority_snapshot TEXT NOT NULL CHECK (json_valid(authority_snapshot)),
  phase TEXT NOT NULL DEFAULT 'writing' CHECK (phase IN ('writing','ready','live','cleanup','deleted')),
  upload_id TEXT,
  sha256 TEXT NOT NULL CHECK (length(sha256)=64),
  byte_count INTEGER NOT NULL CHECK (byte_count BETWEEN 1 AND 10485760),
  created_at INTEGER NOT NULL,
  write_expires_at INTEGER NOT NULL CHECK (write_expires_at > created_at),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX cf_frame_objects_owner ON cf_frame_objects(uid,request_id,phase);
CREATE INDEX cf_frame_objects_cleanup ON cf_frame_objects(phase,next_attempt_at);
CREATE UNIQUE INDEX cf_frame_objects_live_reference ON cf_frame_objects(uid,storage_id) WHERE phase='live';

CREATE TRIGGER IF NOT EXISTS adf_i_frame_objects BEFORE INSERT ON cf_frame_objects
WHEN NEW.phase!='writing' OR NEW.write_expires_at <= unixepoch()
  OR EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
  OR NOT EXISTS (SELECT 1 FROM cf_frame_requests f WHERE f.uid=NEW.uid AND f.request_id=NEW.request_id
    AND f.account_generation=NEW.account_generation AND (
      (NEW.tier='temporary' AND f.state='claimed' AND f.expires_at>unixepoch()) OR
      (NEW.tier='permanent' AND f.state='uploaded' AND f.conversation_id IS NOT NULL)))
BEGIN SELECT RAISE(ABORT,'frame pixel admission changed'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_frame_objects BEFORE UPDATE ON cf_frame_objects
WHEN NEW.uid!=OLD.uid OR NEW.object_id!=OLD.object_id OR NEW.request_id!=OLD.request_id
  OR NEW.storage_id!=OLD.storage_id OR NEW.tier!=OLD.tier OR NEW.account_generation!=OLD.account_generation
  OR NEW.authority_snapshot!=OLD.authority_snapshot OR NEW.sha256!=OLD.sha256 OR NEW.byte_count!=OLD.byte_count
  OR NEW.created_at!=OLD.created_at OR NEW.write_expires_at!=OLD.write_expires_at
  OR (OLD.upload_id IS NOT NULL AND NEW.upload_id IS NOT OLD.upload_id AND NEW.phase!='deleted')
  OR (OLD.phase IN ('cleanup','deleted') AND NEW.phase NOT IN ('cleanup','deleted'))
  OR (NEW.phase='ready' AND (OLD.phase!='writing' OR NEW.upload_id IS NULL))
  OR (NEW.phase='live' AND OLD.phase!='ready')
  OR (NEW.phase NOT IN ('cleanup','deleted') AND (
    EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
    OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
    OR NEW.account_generation!=COALESCE((SELECT account_generation FROM cf_account_cutover WHERE uid=NEW.uid),0)))
BEGIN SELECT RAISE(ABORT,'frame pixel authority changed'); END;

CREATE TRIGGER cf_frame_objects_no_early_delete BEFORE DELETE ON cf_frame_objects
WHEN OLD.phase!='deleted'
BEGIN SELECT RAISE(ABORT,'frame pixel erasure required'); END;

-- Parenthesize CASE expressions: the remote D1 query parser otherwise treats
-- CASE END as the trigger terminator (workers-sdk issue #4727).
-- One statement publishes the object, request, photo and conversation markers.
-- A failed state/quota/photo check rolls back the object's live transition too.
CREATE TRIGGER cf_frame_objects_publish AFTER UPDATE OF phase ON cf_frame_objects
WHEN NEW.phase='live'
BEGIN
  SELECT (CASE WHEN NOT EXISTS (SELECT 1 FROM cf_frame_requests f WHERE f.uid=NEW.uid AND f.request_id=NEW.request_id
    AND f.account_generation=NEW.account_generation AND (
      (NEW.tier='temporary' AND f.state='claimed' AND f.expires_at>unixepoch()) OR
      (NEW.tier='permanent' AND f.state='uploaded' AND EXISTS (
        SELECT 1 FROM cf_conversations c WHERE c.uid=f.uid AND c.id=f.conversation_id))))
    THEN RAISE(ABORT,'frame pixel publication changed') END);
  SELECT (CASE WHEN NEW.tier='temporary' AND NEW.byte_count + COALESCE((SELECT sum(byte_count)
    FROM cf_frame_requests WHERE uid=NEW.uid AND account_generation=NEW.account_generation
    AND device_id=(SELECT device_id FROM cf_frame_requests WHERE uid=NEW.uid AND request_id=NEW.request_id)
    AND state IN ('requested','claimed','uploaded') AND expires_at>unixepoch()),0)>52428800
    THEN RAISE(ABORT,'frame request quota exceeded: bytes') END);
  SELECT (CASE WHEN NEW.tier='permanent' AND EXISTS (SELECT 1 FROM cf_conversations c,
    json_each(c.photos_json) photo WHERE c.uid=NEW.uid AND c.id=(SELECT conversation_id
      FROM cf_frame_requests WHERE uid=NEW.uid AND request_id=NEW.request_id)
    AND json_extract(photo.value,'$.id')=NEW.request_id
    AND json_extract(photo.value,'$.storage_id') IS NOT NEW.storage_id)
    THEN RAISE(ABORT,'conversation photo id is already used') END);
  UPDATE cf_conversations SET photos_json=json_insert(photos_json,'$[#]',json_object(
    'id',NEW.request_id,'base64','','storage_id',NEW.storage_id,'content_type','image/jpeg',
    'description','Just-in-time frame evidence','discarded',json('false'),
    'created_at',(SELECT strftime('%Y-%m-%dT%H:%M:%fZ',created_at,'unixepoch')
      FROM cf_frame_requests WHERE uid=NEW.uid AND request_id=NEW.request_id)))
    WHERE NEW.tier='permanent' AND uid=NEW.uid AND id=(SELECT conversation_id FROM cf_frame_requests
      WHERE uid=NEW.uid AND request_id=NEW.request_id)
    AND NOT EXISTS (SELECT 1 FROM json_each(photos_json) WHERE json_extract(value,'$.id')=NEW.request_id);
  UPDATE cf_conversations SET has_content=1,has_photos=1 WHERE NEW.tier='permanent'
    AND uid=NEW.uid AND id=(SELECT conversation_id FROM cf_frame_requests WHERE uid=NEW.uid AND request_id=NEW.request_id);
  UPDATE cf_frame_requests SET state=(CASE NEW.tier WHEN 'temporary' THEN 'uploaded' ELSE 'attached' END),
    storage_id=NEW.storage_id,byte_count=NEW.byte_count,content_type='image/jpeg',
    uploaded_at=(CASE NEW.tier WHEN 'temporary' THEN unixepoch() ELSE uploaded_at END),
    attached_at=(CASE NEW.tier WHEN 'permanent' THEN unixepoch() ELSE NULL END),
    expires_at=(CASE NEW.tier WHEN 'permanent' THEN created_at ELSE expires_at END),
    cleanup_state=(CASE NEW.tier WHEN 'permanent' THEN 'permanent' ELSE 'not_required' END),
    cleanup_next_attempt_at=NULL WHERE uid=NEW.uid AND request_id=NEW.request_id;
END;

CREATE TRIGGER cf_frame_pixel_metadata_guard BEFORE UPDATE OF storage_id,state,byte_count,content_type ON cf_frame_requests
WHEN NEW.state IN ('uploaded','attached') AND NOT EXISTS (
  SELECT 1 FROM cf_frame_objects o WHERE o.uid=NEW.uid AND o.request_id=NEW.request_id
    AND o.storage_id=NEW.storage_id AND o.phase='live' AND o.byte_count=NEW.byte_count
    AND NEW.content_type='image/jpeg' AND o.tier=(CASE NEW.state WHEN 'attached' THEN 'permanent' ELSE 'temporary' END))
BEGIN SELECT RAISE(ABORT,'frame pixels were not published'); END;

CREATE TRIGGER cf_frame_pixels_cancel AFTER UPDATE OF state,storage_id ON cf_frame_requests
BEGIN
  UPDATE cf_frame_objects SET phase='cleanup',next_attempt_at=0 WHERE uid=NEW.uid AND request_id=NEW.request_id
    AND phase!='deleted' AND NOT (phase='live' AND storage_id=NEW.storage_id AND NEW.state IN ('uploaded','attached'));
END;
CREATE TRIGGER cf_frame_pixels_delete BEFORE DELETE ON cf_frame_requests
BEGIN UPDATE cf_frame_objects SET phase='cleanup',next_attempt_at=0 WHERE uid=OLD.uid AND request_id=OLD.request_id AND phase!='deleted'; END;
CREATE TRIGGER cf_frame_photo_removed AFTER UPDATE OF photos_json ON cf_conversations
BEGIN
  DELETE FROM cf_frame_requests WHERE uid=NEW.uid AND conversation_id=NEW.id AND state='attached'
    AND NOT EXISTS (SELECT 1 FROM json_each(NEW.photos_json) WHERE json_extract(value,'$.id')=cf_frame_requests.request_id
      AND json_extract(value,'$.storage_id')=cf_frame_requests.storage_id);
END;
