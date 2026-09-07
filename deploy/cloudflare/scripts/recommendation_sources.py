"""Project the original WMNow engine; only storage/provider call sites become async."""

import ast
import textwrap

from screen_frame_sources import selected_nodes, source

OWNER = 'backend/utils/task_intelligence/recommendations.py'


def async_engine(name):
    original = selected_nodes(OWNER, {name})
    tree = ast.parse(original)
    data = original.encode('utf-8')
    starts = [0]
    for line in data.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))

    def position(node, end=False):
        return starts[(node.end_lineno if end else node.lineno) - 1] + (node.end_col_offset if end else node.col_offset)

    edits = [(0, 0, b'async ')]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            receiver = node.func.value
            if isinstance(receiver, ast.Name) and receiver.id == 'recommendation_db':
                edits.append(
                    (position(node.func), position(node.func, True), ('await store.' + node.func.attr).encode())
                )
                for keyword in node.keywords:
                    if keyword.arg == 'firestore_client':
                        start, end = position(keyword), position(keyword, True)
                        if data[end : end + 1] == b',':
                            end += 1
                        else:
                            while start and data[start - 1 : start].isspace():
                                start -= 1
                            if data[start - 1 : start] != b',':
                                raise ValueError('upstream store keyword boundary changed')
                            start -= 1
                        edits.append((start, end, b''))
            elif isinstance(receiver, ast.Name) and receiver.id == 'judgment' and node.func.attr == 'judge':
                edits.append(
                    (
                        position(node),
                        position(node, True),
                        b'await store.judge(judgment, shortlist, material_version=material_version, evaluated_at=evaluated_at)',
                    )
                )
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            segment = ast.get_source_segment(original, node) or ''
            if segment.startswith('TASK_INTELLIGENCE_ATTRIBUTION_TOTAL.labels('):
                edits.append(
                    (
                        position(node),
                        position(node, True),
                        b'store.record_intervention_metric(recommendation.feedback_subject_kind.value)',
                    )
                )
    for start, end, value in sorted(edits, reverse=True):
        data = data[:start] + value + data[end:]
    projected = data.decode().replace('firestore_client: Any = None', 'store: Any')
    compile(projected, name, 'exec')
    return projected


def judgment_messages():
    original = source('backend/utils/task_intelligence/live_recommendation_judgment.py')
    cls = next(
        node
        for node in ast.parse(original).body
        if isinstance(node, ast.ClassDef) and node.name == 'LiveRecommendationJudgment'
    )
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == 'judge')
    nodes = [
        node
        for node in method.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id in {'subject_payload', 'instructions', 'payload'}
            for target in node.targets
        )
    ]
    if len(nodes) != 3:
        raise ValueError('upstream WMNow message constructor changed')
    lines = original.splitlines()
    statements = '\n\n'.join(textwrap.dedent('\n'.join(lines[node.lineno - 1 : node.end_lineno])) for node in nodes)
    return (
        'def judgment_messages(subjects):\n'
        + textwrap.indent(statements, '    ')
        + "\n    return [{'role':'system','content':instructions}, {'role':'user','content':payload}]\n"
    )


def outcome_policy():
    original = selected_nodes('backend/database/task_recommendations.py', {'_outcome_matches_chain'})
    tree = ast.parse(original)
    function = tree.body[0]
    function.args.args[0].arg = 'store'
    reads = {
        "user_ref.collection('candidates').document(source_id).get()": 'store.candidate(source_id)',
        "user_ref.collection('action_items').document(result_task_id).get()": 'store.task(result_task_id)',
        "user_ref.collection('action_items').document(source_id).get()": 'store.task(source_id)',
        "user_ref.collection('workstreams').document(workstream_id).collection('artifact_refs').document(request.subject_id).get()": 'store.artifact(workstream_id, request.subject_id)',
    }
    seen = set()

    class StorageCalls(ast.NodeTransformer):
        def visit_Call(self, node):
            token = ast.unparse(node)
            if token in reads:
                seen.add(token)
                return ast.copy_location(ast.Await(ast.parse(reads[token], mode='eval').body), node)
            if isinstance(node.func, ast.Name) and node.func.id == '_snapshot_dict':
                return self.visit(node.args[0])
            return self.generic_visit(node)

        def visit_Attribute(self, node):
            if node.attr == 'exists':
                return ast.copy_location(ast.Compare(self.visit(node.value), [ast.IsNot()], [ast.Constant(None)]), node)
            return self.generic_visit(node)

    result = StorageCalls().visit(function)
    if seen != set(reads):
        raise ValueError('upstream outcome storage boundary changed')
    result = ast.AsyncFunctionDef(**result.__dict__)
    ast.fix_missing_locations(result)
    return ast.unparse(result) + '\n'


def recommendation_sources():
    pure = {
        'MAX_LOCAL_SNAPSHOT_TTL',
        'SnapshotValidationError',
        '_validate_snapshot_window',
        'MAX_SHORTLIST_SIZE',
        'MAX_RECOMMENDATIONS',
        'ATTENTION_TIER_RESERVED_CAPACITY',
        'PROJECTION_TTL',
        'PROMPT_VERSION',
        'POLICY_VERSION',
        'FACT_DEFINITION_VERSION',
        'JudgmentSelection',
        'EvaluationSubject',
        'RecommendationJudgment',
        '_stable_id',
        '_recommendation_dedupe_key',
        'candidate_recommendation_dedupe_key',
        '_as_aware',
        '_iso_token',
        '_valid_evidence',
        '_context_signals',
        '_days_to_due',
        '_stored_confidence',
        '_recent',
        '_eligibility',
        '_canonical_evidence_preview',
        '_subject',
        'valid_evidence',
        'build_evaluation_subject',
        '_build_subjects',
        '_attention_tier',
        '_round_robin',
        '_balanced_tier',
        'filter_shortlist',
        '_material_version',
    }
    header = (
        'from __future__ import annotations\nimport hashlib,json\nfrom collections import defaultdict,deque\n'
        'from dataclasses import dataclass\nfrom datetime import datetime,timedelta,timezone\n'
        'from typing import Any,Optional,Protocol\nfrom pydantic import BaseModel,ConfigDict,Field\n'
        'import candidate_kernel_policy as candidates_db\n'
        'from candidate_kernel_models import CandidateAction,CandidateStatus,CandidateSubjectKind\n'
        'from memory_kernel_action_item import EvidenceKind,EvidenceRef,EvidenceScope,TaskOwner,TaskStatus\n'
        'from candidate_kernel_goal import GoalStatus\n'
        'from candidate_kernel_recommendation import *\n'
        'from candidate_kernel_suggested import MINIMUM_CAPTURE_CONFIDENCE\n\n'
    )
    schema = selected_nodes('backend/utils/task_intelligence/live_recommendation_judgment.py', {'JudgmentOutput'})
    return {
        'recommendation_kernel.py': header
        + selected_nodes(OWNER, pure)
        + schema
        + judgment_messages()
        + async_engine('evaluate')
        + async_engine('get_debug_projection')
        + outcome_policy()
    }
