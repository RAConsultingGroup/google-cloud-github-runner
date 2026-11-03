"""
GitHub Client for authenticating and interacting with the GitHub API.
"""
import os
import threading
import time
from datetime import datetime
import jwt
import requests
import logging

REQUEST_TIMEOUT = 30  # seconds
# Installation and registration tokens are valid for one hour; reusing them for most of that window
# turns three GitHub round trips per queued-job webhook into one, which matters during job bursts.
TOKEN_TTL = 50 * 60  # seconds

logger = logging.getLogger(__name__)

_token_cache = {}
_token_cache_lock = threading.Lock()


def _cached_token(key, fetch):
    """Return a cached token for key, fetching a new one when missing or older than TOKEN_TTL."""
    with _token_cache_lock:
        cached = _token_cache.get(key)
        if cached and cached[1] > time.time():
            return cached[0]
    token = fetch()
    with _token_cache_lock:
        _token_cache[key] = (token, time.time() + TOKEN_TTL)
    return token


def clear_token_cache():
    """Drop cached tokens (tests, and after a credential rotation)."""
    with _token_cache_lock:
        _token_cache.clear()


class GitHubClient:
    """Client for authenticated interactions with the GitHub API as a GitHub App."""

    def __init__(self):
        """Initialize GitHubClient with environment configuration."""
        self.app_id = os.environ.get('GITHUB_APP_ID')
        self.installation_id = os.environ.get('GITHUB_INSTALLATION_ID')
        self.private_key = os.environ.get('GITHUB_PRIVATE_KEY')
        self.private_key_path = os.environ.get('GITHUB_PRIVATE_KEY_PATH')
        self.project_id = os.environ.get('GOOGLE_CLOUD_PROJECT')

        if not all([self.app_id, self.installation_id]) or not (self.private_key_path or self.private_key):
            logger.warning("GitHub App configuration missing.")

    def _get_private_key(self):
        """
        Retrieve the GitHub App private key.

        Returns:
            str: The private key content.

        Raises:
            ValueError: If no private key source is configured.
        """
        # Retrun environment variable
        if self.private_key:
            return self.private_key
        # Return file content
        elif self.private_key_path:
            with open(self.private_key_path, 'r') as f:
                return f.read()
        else:
            raise ValueError("No private key source configured.")

    def _generate_jwt(self):
        """Generates a JWT for GitHub App authentication."""
        try:
            private_key = self._get_private_key()

            payload = {
                'iat': int(time.time()),
                'exp': int(time.time()) + (10 * 60),
                'iss': self.app_id
            }

            encoded_jwt = jwt.encode(payload, private_key, algorithm='RS256')
            return encoded_jwt
        except Exception as e:
            logger.error(f"Error generating JWT: {e}")
            raise

    @staticmethod
    def _headers(token):
        return {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28'
        }

    def get_installation_access_token(self):
        """Obtains an installation access token, reused for most of its one-hour lifetime."""
        return _cached_token(('installation', self.installation_id), self._fetch_installation_access_token)

    def _fetch_installation_access_token(self):
        # https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app
        headers = self._headers(self._generate_jwt())
        url = f'https://api.github.com/app/installations/{self.installation_id}/access_tokens'

        response = requests.post(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        # The installation access token will expire after 1 hour.
        return response.json()['token']

    def get_registration_token(self, org_name=None, repo_name=None, delivery_id=None):
        """Gets a runner registration token, reused for most of its one-hour lifetime (it can register many runners)."""
        return _cached_token(
            ('registration', org_name, repo_name),
            lambda: self._fetch_registration_token(org_name=org_name, repo_name=repo_name, delivery_id=delivery_id),
        )

    def _fetch_registration_token(self, org_name=None, repo_name=None, delivery_id=None):
        # https://docs.github.com/en/rest/actions/self-hosted-runners
        headers = self._headers(self.get_installation_access_token())
        if org_name:
            # GitHub Docs: https://t.ly/dAyGK
            url = f"https://api.github.com/orgs/{org_name}/actions/runners/registration-token"
            logger.info(
                "Create registration token for organization: %s, delivery_id: %s",
                org_name,
                delivery_id,
            )
        elif repo_name:
            # GitHub Docs: https://t.ly/n0w2a
            url = f"https://api.github.com/repos/{repo_name}/actions/runners/registration-token"
            logger.info(
                "Create registration token for repository: %s, delivery_id: %s",
                repo_name,
                delivery_id,
            )
        else:
            raise ValueError("Either org_name or repo_name must be provided")

        response = requests.post(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()['token']

    def list_org_runners(self, org_name):
        """List the organization's self-hosted runners (id, name, status, busy)."""
        # https://docs.github.com/en/rest/actions/self-hosted-runners#list-self-hosted-runners-for-an-organization
        headers = self._headers(self.get_installation_access_token())
        url = f"https://api.github.com/orgs/{org_name}/actions/runners"
        params = {'per_page': 100}
        runners = []
        while url:
            response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            runners.extend(response.json()['runners'])
            url = response.links.get('next', {}).get('url')
            params = None
        return runners

    def find_org_runner(self, org_name, runner_name):
        """Look up one runner by name, independent of the paged listing. Returns it, or None if not registered."""
        # https://docs.github.com/en/rest/actions/self-hosted-runners#list-self-hosted-runners-for-an-organization
        url = f"https://api.github.com/orgs/{org_name}/actions/runners"
        response = requests.get(
            url, headers=self._headers(self.get_installation_access_token()),
            params={'name': runner_name}, timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return next((runner for runner in response.json()['runners'] if runner['name'] == runner_name), None)

    def delete_org_runner(self, org_name, runner_id):
        """Remove a runner registration. Returns False when GitHub refuses because the runner is running a job."""
        # https://docs.github.com/en/rest/actions/self-hosted-runners#delete-a-self-hosted-runner-from-an-organization
        url = f"https://api.github.com/orgs/{org_name}/actions/runners/{runner_id}"
        response = requests.delete(url, headers=self._headers(self.get_installation_access_token()), timeout=REQUEST_TIMEOUT)
        if response.status_code == 422:
            return False
        if response.status_code == 404:
            return True
        response.raise_for_status()
        return True

    def list_deliveries(self, since, status, max_pages=10):
        """List app webhook deliveries with status 'success' or 'failure' delivered at or after since, newest first."""
        # https://docs.github.com/en/rest/apps/webhooks#list-deliveries-for-an-app-webhook
        headers = self._headers(self._generate_jwt())
        url = "https://api.github.com/app/hook/deliveries"
        params = {'per_page': 100, 'status': status}
        deliveries = []
        for _ in range(max_pages):
            response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            page = response.json()
            recent = [d for d in page if datetime.fromisoformat(d['delivered_at']) >= since]
            deliveries.extend(recent)
            url = response.links.get('next', {}).get('url')
            if not url or len(recent) < len(page):
                break
            params = None
        return deliveries

    def redeliver(self, delivery_id):
        """Ask GitHub to send an app webhook delivery again."""
        # https://docs.github.com/en/rest/apps/webhooks#redeliver-a-delivery-for-an-app-webhook
        url = f"https://api.github.com/app/hook/deliveries/{delivery_id}/attempts"
        response = requests.post(url, headers=self._headers(self._generate_jwt()), timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
