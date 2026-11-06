"""Podcast conflict preference; identity, missing-state and write guards stay with callers."""

PRIMARY_SOURCES = ('auto', 'pinepods', 'abs', 'jf')


def validate_primary(value):
    if not isinstance(value, str) or value not in PRIMARY_SOURCES:
        raise ValueError('Invalid primary podcast source')
    return value


def preferred_change(states, changed, primary):
    """Only arbitrate competing changes. An unchanged primary never undoes a new listen."""
    validate_primary(primary)
    if primary != 'auto' and primary in changed and not states[primary].get('missing'):
        return primary
    return None
