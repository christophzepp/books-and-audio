"""In-process diagnostic counters. Recording a counter never touches media or state."""


def increment(owner, name, amount=1):
    if not isinstance(name, str) or not name.replace('_', '').isalnum() or type(amount) is not int or amount < 0:
        raise ValueError('Invalid I/O counter')
    values = getattr(owner, '_io_metrics', None)
    if values is None:
        values = {}
        owner._io_metrics = values
    values[name] = values.get(name, 0) + amount
    from media_activity import COUNTERS, count
    if name in COUNTERS:
        count(COUNTERS[name], amount)


def snapshot(owner):
    return dict(getattr(owner, '_io_metrics', {}))
