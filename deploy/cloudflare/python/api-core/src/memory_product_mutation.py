"""Upstream ordinary visibility, review and desktop product-field policies."""

from memory_apply_mutation import apply_user_memory_mutation
from memory_apply_edit import content_edit_patch
from memory_apply_intake import encoded


def visibility_patch(item, now, value):
    return {'target_visibility': value}, {}, {}


def review_patch(item, now, value):
    promotion = dict(item.promotion or {})
    promotion.update(reviewed=True, user_review=value)
    if promotion.get('required') is True and item.processing_state.value == 'pending':
        if value and promotion.get('processing_status') == 'processing_rejected':
            promotion['processing_status'] = 'pending_processing'
        elif not value:
            promotion['processing_status'] = 'processing_rejected'
    patch = {'promotion_audit': promotion}
    if not value:
        patch['kg_extracted'] = False
    return {}, patch, {'reviewed': 1, 'user_review': int(value)}


def product_patch(item, now, values):
    if not values or not set(values).issubset({'is_read', 'is_dismissed', 'is_baseline'}):
        raise ValueError('invalid product memory fields')
    if any(not isinstance(value, bool) for value in values.values()):
        raise ValueError('invalid product memory flag')
    promotion = {**(item.promotion or {}), **values}
    return {}, {'promotion_audit': promotion}, {key: int(value) for key, value in values.items()}


async def mutate_visibility(env, uid, memory_id, value, now):
    if value not in {'private', 'public'}:
        raise ValueError('invalid memory visibility')
    return await apply_user_memory_mutation(
        env,
        uid,
        memory_id,
        now,
        kind='visibility',
        build_patch=lambda item, instant: visibility_patch(item, instant, value),
    )


async def mutate_review(env, uid, memory_id, value, now, *, feedback):
    return await apply_user_memory_mutation(
        env,
        uid,
        memory_id,
        now,
        kind=f'review:{value}',
        build_patch=lambda item, instant: review_patch(item, instant, value),
        extra_statements=[feedback],
    )


async def mutate_product_fields(env, uid, memory_id, values, now):
    return await apply_user_memory_mutation(
        env,
        uid,
        memory_id,
        now,
        kind='product_metadata',
        build_patch=lambda item, instant: product_patch(item, instant, values),
    )


async def mutate_external_fields(env, uid, memory_id, values, now):
    if not values or not set(values).issubset({'content', 'visibility', 'tags', 'category'}):
        raise ValueError('invalid external memory fields')

    def build(item, instant):
        logical, patch, physical = {}, {}, {}
        if 'content' in values:
            content = values['content'].strip()
            if not content:
                raise ValueError('invalid external memory content')
            patch = content_edit_patch(item, content, instant)
            logical.update(
                memory_text=content, target_tier='short_term', target_user_asserted=True, clear_graph_assertion=True
            )
            physical.update(edited=1, reviewed=1, user_review=1)
        if 'visibility' in values:
            if values['visibility'] not in {'public', 'private'}:
                raise ValueError('invalid external memory visibility')
            logical['target_visibility'] = values['visibility']
        metadata = {key: values[key] for key in ('tags', 'category') if key in values}
        if metadata:
            patch['promotion_audit'] = {**patch.get('promotion_audit', item.promotion or {}), **metadata}
            physical.update(
                {
                    ('tags_json' if key == 'tags' else key): encoded(value) if key == 'tags' else value
                    for key, value in metadata.items()
                }
            )
        return logical, patch, physical

    return await apply_user_memory_mutation(
        env,
        uid,
        memory_id,
        now,
        kind='external_memory_update',
        build_patch=build,
    )
