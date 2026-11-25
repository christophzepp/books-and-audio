"""Match the current BookOrbit client to the operator-enrolled Docker instance."""
import re
from bookorbit_scope import read_library

from watch_deployment import (
    BOOKORBIT_VERSION, IMAGE_PATTERN, DeploymentError, bookorbit_instance_policy, bookorbit_policy_digest,
)


def verified_receipt(gate, policy):
    receipt = gate.get('bookorbit_instance')
    if (not isinstance(receipt, dict) or set(receipt) != {'container_id', 'image', 'started_at', 'version', 'profile_digest'}
            or receipt['profile_digest'] != bookorbit_policy_digest(policy)
            or not isinstance(receipt['container_id'], str) or not re.fullmatch(r'[0-9a-f]{64}', receipt['container_id'])
            or not isinstance(receipt['image'], str) or not IMAGE_PATTERN.fullmatch(receipt['image'])
            or not isinstance(receipt['version'], str) or not BOOKORBIT_VERSION.fullmatch(receipt['version'])
            or not isinstance(receipt['started_at'], str) or not receipt['started_at']):
        raise DeploymentError('BookOrbit instance verification is missing or belongs to another enrollment')
    return receipt


def verify_api_instance(bridge, gate):
    config, client = bridge.config, bridge.bo
    policy = bookorbit_instance_policy(config)
    receipt = verified_receipt(gate, policy)
    origin = policy['api_origin'].rstrip('/')
    if client is None or config.get('bo_url', '').rstrip('/') != origin or client.base_url != origin:
        raise DeploymentError('BookOrbit API origin differs from the enrolled instance')
    scope = config.get('auto_discovery', {}).get('bo_library_ids', [])
    if not isinstance(scope, list) or any(type(ident) is not int or ident <= 0 for ident in scope):
        raise DeploymentError('Invalid BookOrbit library scope')
    scoped_ids = set(scope)
    ebooks = config.get('jellyfin', {}).get('auto_ebooks', {})
    if ebooks.get('enabled'):
        ident = ebooks.get('bo_library_id')
        if type(ident) is not int or ident <= 0:
            raise DeploymentError('Invalid BookOrbit EPUB library scope')
        scoped_ids.add(ident)
    if scoped_ids != {row['id'] for row in policy['libraries']}:
        raise DeploymentError('BookOrbit library scope differs from its enrollment')
    user_id = str(policy['user_id'])
    client.expected_user_id = user_id
    roots = tuple((row['id'], tuple(sorted(row['roots']))) for row in policy['libraries'])
    identity = (receipt['profile_digest'], receipt['container_id'], receipt['started_at'], receipt['image'],
                receipt['version'], user_id, roots)
    verified = getattr(bridge, '_verified_bookorbit_instance', None)
    current_user = client.user if isinstance(client.user, dict) else {}
    if (verified == (identity, gate['checked_at']) and client._authenticated
            and str(current_user.get('id')) == user_id):
        return False
    user = client._request('GET', '/api/v1/auth/me', '/api/v1/auth/me')
    if (not isinstance(user, dict) or str(user.get('id')) != user_id
            or not isinstance(client.user, dict) or str(client.user.get('id')) != user_id):
        raise DeploymentError('BookOrbit authentication differs from the enrolled account')
    for expected in policy['libraries']:
        if read_library(client,expected['id'])['roots'] != sorted(expected['roots']):
            raise DeploymentError('BookOrbit library roots differ from their enrollment')
    bridge._verified_bookorbit_instance = (identity, gate['checked_at'])
    return True
