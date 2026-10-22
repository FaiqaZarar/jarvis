"""The only package allowed to run SQL against the cloud database - see
docs/coding-guides/database-access.md (jarvis_server's version of the same
convention) for the shape every module here follows."""

from jarvis_cloud_server.internal.db.connection import get_connection, get_pool
from jarvis_cloud_server.internal.db.users import get_or_create_user, get_user_by_id
from jarvis_cloud_server.internal.db.login_requests import (
    claim_session_delivery,
    confirm_and_consume,
    count_recent_for_email,
    create_login_request,
    get_login_request,
    set_user_id,
)
from jarvis_cloud_server.internal.db.sessions import create_session, get_active_session_by_token_hash
from jarvis_cloud_server.internal.db.feedback import SEVERITIES, insert_feedback
from jarvis_cloud_server.internal.db.auto_error_reports import record_error_report, set_github_issue_url
