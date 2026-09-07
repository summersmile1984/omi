"""Original recurrence qualification, proposal and stable identities for Workers."""

import ast
from screen_frame_sources import selected_nodes

OWNER = 'backend/utils/task_intelligence/workstream_association.py'


def recurrence_sources():
    original = ast.parse(selected_nodes(OWNER, {'consume_recurrence_signal'})).body[0]
    original.args = ast.parse('def consume(store, uid, signal, *, account_generation=0): pass').body[0].args
    seen = set()

    class StorageCalls(ast.NodeTransformer):
        def visit_Call(self, node):
            target = ast.unparse(node.func)
            if target == 'workstreams_db.get_task_workflow_control':
                seen.add('control')
                return ast.copy_location(ast.Await(ast.parse('store.control(uid)', mode='eval').body), node)
            if target == 'create_candidate':
                seen.add('candidate')
                node.func = ast.parse('store.create_candidate', mode='eval').body
                return ast.copy_location(ast.Await(self.generic_visit(node)), node)
            return self.generic_visit(node)

        def visit_Attribute(self, node):
            if ast.unparse(node) == 'recurrence_inbox_db.RecurrenceGenerationMismatchError':
                return ast.copy_location(ast.Name('CandidateGenerationMismatchError', ast.Load()), node)
            return self.generic_visit(node)

    result = StorageCalls().visit(original)
    if seen != {'control', 'candidate'}:
        raise ValueError('upstream recurrence storage boundary changed')
    result = ast.AsyncFunctionDef(**result.__dict__)
    ast.fix_missing_locations(result)
    header = (
        'from __future__ import annotations\nimport hashlib\n'
        'from memory_kernel_recurrence import CanonicalRecurrenceSignal\n'
        'from memory_kernel_action_item import TaskCreatePayload\n'
        'from candidate_kernel_models import CandidateCreate,WorkstreamCreateCandidate,WorkstreamProposal\n'
        'from candidate_kernel_policy import CandidateGenerationMismatchError\n'
        'from candidate_kernel_association import RecurrenceConsumptionOutcome,RecurrenceOutcomeKind\n\n'
    )
    return {
        'recurrence_kernel.py': header
        + selected_nodes(
            OWNER,
            {
                'RECURRENCE_POLICY_VERSION',
                'RECURRENCE_MIN_OCCURRENCES',
                'RECURRENCE_MIN_DISTINCT_DAYS',
                'RECURRENCE_MIN_CONFIDENCE',
                '_recurrence_idempotency_key',
            },
        )
        + selected_nodes('backend/database/recurrence_inbox.py', {'_receipt_id'})
        + ast.unparse(result)
        + '\n'
    }
