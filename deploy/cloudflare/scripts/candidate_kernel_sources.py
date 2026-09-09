"""Project upstream Candidate identities, wire models and pure storage policies."""

from screen_frame_sources import selected_nodes


def candidate_sources():
    policy = (
        'from __future__ import annotations\n'
        'import hashlib, json\n'
        'from datetime import datetime, timedelta, timezone\n'
        'from typing import Any, Optional\n'
        'import candidate_kernel_text as action_items_db\n'
        'from memory_kernel_action_item import EvidenceRef, TaskChangePayload, TaskCreatePayload, TaskOwner, TaskPriority, TaskStatus\n'
        'from candidate_kernel_models import CandidateAction, CandidateCompatibilityMetadata, CandidateCreate, CandidateRecord, CandidateStatus, CandidateSubjectKind\n\n'
        + selected_nodes(
            'backend/database/candidates.py',
            {
                'CandidateStoreError',
                'CandidateNotFoundError',
                'CandidateConflictError',
                'CandidateGenerationMismatchError',
                'SUGGESTION_TTL',
                'PENDING_CANDIDATE_SEMANTIC_VERSION',
                'WORKSTREAM_CANDIDATE_SEMANTIC_VERSION',
                'MAX_CANDIDATE_EVIDENCE_REFS',
                'TASK_PRIORITY_RANK',
                '_max_optional_confidence',
                '_strongest_task_priority',
                '_stable_contract_id',
                'candidate_id_for_idempotency',
                'task_id_for_candidate',
                '_canonical_request_value',
                '_proposal_request_hash',
                '_semantic_due_value',
                '_task_create_semantic_payload',
                'pending_candidate_semantic_identity',
                'suggested_candidate_semantic_identity',
                '_pending_semantic_claim_id',
                '_merge_candidate_annotations',
                '_merge_task_provenance',
                '_accepted_task_is_active',
                '_accepted_task_matches_semantic_claim',
                '_stored_confidence',
                '_stored_optional_confidence',
                '_stored_task_priority',
                '_as_utc',
                '_suggestion_window_has_closed',
                'candidate_has_lapsed',
                'stored_candidate_has_lapsed',
                '_task_create_storage',
                '_task_update_storage',
            },
        )
    )
    return {
        'candidate_kernel_policy.py': policy,
        'candidate_kernel_suggested.py': (
            'from __future__ import annotations\nimport hashlib\n'
            'from datetime import datetime, timedelta, timezone\nfrom typing import Optional\n'
            'import candidate_kernel_policy as candidates_db\n'
            'from candidate_kernel_models import CandidateRecord, CandidateAction, CandidateStatus, CandidateSubjectKind\n'
            'from memory_kernel_action_item import TaskCreatePayload\n\n'
            + selected_nodes('backend/utils/task_intelligence/capture_policy.py', {'MINIMUM_CAPTURE_CONFIDENCE'})
            + selected_nodes(
                'backend/utils/task_intelligence/recommendations.py',
                {
                    '_stable_id',
                    'candidate_recommendation_dedupe_key',
                    'DEFAULT_LATER_TTL',
                    'DISMISS_TTL',
                },
            )
            + selected_nodes(
                'backend/routers/candidates.py',
                {
                    'SUGGESTED_CANDIDATE_LIMIT',
                    'SUGGESTED_CANDIDATE_RAW_LIMIT',
                    '_has_suggested_candidate_shape',
                    '_is_suggested_candidate',
                    '_suggested_candidates',
                },
            )
        ),
        'candidate_kernel_attention.py': (
            'from __future__ import annotations\nimport hashlib, json\nfrom typing import Any\n\n'
            + selected_nodes('backend/database/task_recommendations.py', {'_request_hash'})
        ),
        'candidate_kernel_text.py': (
            'from typing import Optional\n\n'
            + selected_nodes(
                'backend/database/action_items.py', {'_normalize_description', 'normalize_action_item_description'}
            )
        ),
        'candidate_kernel_staged.py': (
            'from __future__ import annotations\nfrom datetime import datetime, timezone\nfrom typing import Any\n'
            'from memory_kernel_action_item import EvidenceKind, EvidenceRef, EvidenceScope, TaskCreatePayload, TaskOwner, TaskPriority\n'
            'from pydantic import BaseModel, Field\nfrom typing import List\n'
            'from candidate_kernel_models import CandidateAction, CandidateCompatibilityMetadata, CandidateCreate, CandidateSubjectKind, CandidateRecord, CandidateStatus\n\n'
            + selected_nodes(
                'backend/utils/task_intelligence/staged_migration.py', {'_aware', 'proposal_from_legacy_staged'}
            )
            + selected_nodes(
                'backend/routers/staged_tasks.py',
                {
                    '_LEGACY_EVIDENCE_PREFIX',
                    '_MISSING_TIMESTAMP',
                    '_candidate_as_staged',
                    '_legacy_as_staged',
                    '_candidate_legacy_row_ids',
                    '_is_staged_compatibility_candidate',
                    '_projected_timestamp',
                    '_staged_sort_key',
                    'CreateStagedTaskRequest',
                    'BatchScoreEntry',
                    'BatchUpdateScoresRequest',
                },
            )
        ),
        'candidate_kernel_workflow.py': (
            'from __future__ import annotations\nimport hashlib\nfrom datetime import datetime\nfrom typing import Any, Optional\n'
            'from memory_kernel_action_item import TaskOwner, TaskPriority, TaskStatus\n'
            'from candidate_kernel_workstream import Workstream, WorkstreamStatus, WorkstreamEvent, WorkstreamEventKind, WorkstreamSensitivity\n\n'
            + selected_nodes(
                'backend/database/workstreams.py',
                {'_stable_id', '_task_storage', '_workstream_storage', '_initial_event_storage'},
            )
        ),
    }
