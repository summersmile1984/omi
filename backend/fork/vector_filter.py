"""Translate the upstream vector filter contract to Qdrant payload predicates.

Payload values live under metadata; values_count preserves field presence
even for empty arrays. Unknown syntax fails rather than dropping a
predicate, particularly the account ownership predicate.
"""

import math
import re


def _field(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
        raise ValueError('unsupported vector metadata field')
    return 'metadata.' + name


def _match(key, value):
    if isinstance(value, (str, bool)) or type(value) is int:
        return {'key': key, 'match': {'value': value}}
    if type(value) is float and math.isfinite(value):
        return {'key': key, 'range': {'gte': value, 'lte': value}}
    raise ValueError('unsupported vector filter value')


def translate(filters):
    if not isinstance(filters, dict) or not filters:
        raise ValueError('vector operation requires a nonempty metadata filter')
    clauses = []
    for name, expressions in filters.items():
        if name in ('$and', '$or'):
            if not isinstance(expressions, list) or not expressions:
                raise ValueError('vector logical filter requires operands')
            clauses.append({'must' if name == '$and' else 'should': [translate(part) for part in expressions]})
            continue
        key = _field(name)
        if not isinstance(expressions, dict):
            expressions = {'$eq': expressions}
        if not expressions:
            raise ValueError('empty vector field filter')
        for operation, value in expressions.items():
            if operation == '$eq':
                clauses.append(_match(key, value))
            elif operation in ('$gt', '$gte', '$lt', '$lte'):
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError('vector range filter requires a finite number')
                clauses.append({'key': key, 'range': {operation[1:]: value}})
            elif operation == '$in':
                if not isinstance(value, list):
                    raise ValueError('vector membership filter requires an array')
                # Qdrant empty should has special logical semantics. An empty
                # positive membership must be false, never an unrestricted match.
                condition = {'should': [_match(key, item) for item in value]} if value else {'has_id': []}
                clauses.append(condition)
            elif operation == '$exists':
                if type(value) is not bool:
                    raise ValueError('vector exists filter requires a boolean')
                condition = {'key': key, 'values_count': {'gte': 0}}
                clauses.append(condition if value else {'must_not': [condition]})
            else:
                raise ValueError('unsupported vector filter operator')
    return {'must': clauses}
