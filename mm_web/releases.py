"""Single adapter boundary for the stabilize campaign machinery."""
from __future__ import annotations
import importlib
import monitor
from mm_web import projections
from mm_web.errors import ApiError
from mm_web.models import Campaign, Resolution
from mm_web.security import scrub


def is_error(module, exc, name):
    cls = getattr(module, name, None)
    return isinstance(exc, cls) if isinstance(cls, type) else type(exc).__name__ == name


def call(name, *args, **kwargs):
    try:
        module = importlib.import_module('stabilize')
    except ImportError:
        raise ApiError(503, 'release_machinery_unavailable', 'Release machinery is not connected') from None
    try:
        return getattr(module, name)(*args, **kwargs)
    except Exception as exc:
        if is_error(module, exc, 'CampaignAlreadyLive'):
            raise ApiError(409, 'release_conflict', 'A release campaign is already live',
                           campaign_id=scrub(getattr(exc, 'campaign_id', None))) from None
        if is_error(module, exc, 'CampaignNotFound'):
            raise ApiError(404, 'release_not_found', 'Release campaign was not found') from None
        if is_error(module, exc, 'InvalidRef') or isinstance(exc, ValueError):
            raise ApiError(400, 'invalid_release', 'Release request is invalid') from None
        if is_error(module, exc, 'StabilizeError'):
            raise ApiError(502, 'release_failed', scrub(str(exc))[:1000]) from None
        raise ApiError(503, 'release_machinery_unavailable', 'Release machinery is unavailable') from None


def campaign(value):
    value = projections.campaign(value)
    value['id'] = value.get('id', value.get('campaign_id'))
    value['tip_sha'] = value.get('tip_sha', value.get('sha'))
    return Campaign.model_validate(value)


def resolve(commitish=None):
    raw = call('resolve', commitish)
    value = projections.fields(raw, 'sha subject committed_at on_master live_campaign_id')
    value['push_ci'] = projections.fields(raw.get('push_ci'), 'run_id url conclusion')
    return Resolution(**{**value, 'commitish': scrub(commitish),
        'url': f'https://github.com/{monitor.REPO_NAME}/commit/{value["sha"]}'})


def create(sha, login, idempotency_key):
    return campaign(call('create', sha=sha, requested_by=login, idempotency_key=idempotency_key))


def get(campaign_id):
    value = call('get', campaign_id)
    if value is None:
        raise ApiError(404, 'release_not_found', 'Release campaign was not found')
    return campaign(value)


def list_campaigns(limit=50):
    return [campaign(value) for value in call('list_campaigns', limit=limit)]


def abort(campaign_id, login):
    return campaign(call('abort', campaign_id, requested_by=login))
