"""
Route Cloud Scheduler calls to reconcile runner VMs with GitHub.
"""
import logging
import os
from flask import Blueprint, request, jsonify
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from app.services import ReconcileService
from app import limiter

logger = logging.getLogger(__name__)

reconcile_bp = Blueprint('reconcile', __name__)


def _is_scheduler(authorization):
    """Accept only an OIDC token minted for RECONCILE_AUDIENCE by the RECONCILE_INVOKER service account."""
    token = authorization.removeprefix('Bearer ').strip()
    if not token:
        return False
    try:
        claims = id_token.verify_oauth2_token(
            token, google_requests.Request(), audience=os.environ['RECONCILE_AUDIENCE']
        )
    except ValueError as e:
        logger.warning("Rejected reconcile token: %s", e)
        return False
    return claims.get('email') == os.environ['RECONCILE_INVOKER'] and claims.get('email_verified') is True


@reconcile_bp.route('/reconcile', methods=['POST'])
# Same policy as /webhook: only rejected requests count against the limit, so the every-minute
# scheduler call is never throttled.
@limiter.limit("1000 per hour", deduct_when=lambda response: response.status_code >= 400)
def reconcile():
    """Run one reconciliation pass. Disabled (404) unless RECONCILE_INVOKER is configured."""
    if not os.environ.get('RECONCILE_INVOKER'):
        return jsonify({'status': 'not found'}), 404
    if not _is_scheduler(request.headers.get('Authorization', '')):
        return jsonify({'status': 'forbidden'}), 403
    result = ReconcileService().run()
    return jsonify({'status': 'success', **result}), 200
