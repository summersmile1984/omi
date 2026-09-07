"""A review decision is part of the canonical item or privacy transaction."""

from dataclasses import dataclass


class ReviewSourceChanged(ValueError):
    pass


def review_source_matches(review, item):
    return (
        item.memory_id == review['fact_id']
        and item.status.value == 'active'
        and item.ledger_commit_id == review['source_commit_id']
        and item.item_revision == review['source_item_revision']
        and item.content_hash == review['source_content_hash']
        and (item.promotion or {}).get('route') == 'review'
    )


@dataclass(frozen=True)
class CanonicalReviewResolution:
    review: dict
    decision: str
    reason: str = ''

    def validate(self, item):
        if self.decision not in {'accept', 'correct', 'reject', 'drop'}:
            raise ValueError('invalid canonical review decision')
        if self.review['status'] not in {'pending', 'pending_review'} or not review_source_matches(self.review, item):
            raise ReviewSourceChanged('memory_review_source_changed')

    def admission(self, db, uid):
        row = self.review
        return db.prepare(
            'INSERT INTO cf_memory_review_apply_guard '
            '(uid, review_id, memory_id, decision, source_commit_id, source_item_revision, source_content_hash) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)'
        ).bind(
            uid,
            row['review_id'],
            row['fact_id'],
            self.decision,
            row['source_commit_id'],
            row['source_item_revision'],
            row['source_content_hash'],
        )

    def resolution_statements(self, db, uid, commit_id, now):
        status = {'accept': 'accepted', 'correct': 'accepted', 'reject': 'rejected', 'drop': 'dropped'}[self.decision]
        reason = 'canonical_review_' + self.decision if self.decision in {'reject', 'drop'} else self.reason
        # The retained 0093 schema has NOT NULL source columns. Empty strings,
        # revision 1 and impact 0 are storage sentinels only; the wire projection
        # exposes null after resolution. No prior hash or candidate survives.
        return [
            db.prepare(
                "UPDATE cf_memory_review_queue SET previous_status = status, status = ?, decision = ?, reason = ?, "
                "resolution_commit_id = ?, candidate_json = '{}', source_commit_id = '', source_item_revision = 1, "
                "source_content_hash = '', source_short_term_id = NULL, veracity = NULL, impact = 0, "
                "correction_json = NULL, permitted_uses_json = '[]', resolved_at = ?, updated_at = ? "
                "WHERE uid = ? AND review_id = ? AND status IN ('pending', 'pending_review')"
            ).bind(status, self.decision, reason, commit_id, now, now, uid, self.review['review_id']),
            db.prepare('DELETE FROM cf_memory_review_apply_guard WHERE uid = ?').bind(uid),
        ]
