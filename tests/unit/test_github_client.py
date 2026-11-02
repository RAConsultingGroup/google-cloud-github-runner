import pytest
import logging
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from app.clients.github_client import GitHubClient


@pytest.fixture
def mock_env_vars(monkeypatch):
    """Set up mock environment variables for GitHub client."""
    monkeypatch.setenv('GITHUB_APP_ID', '12345')
    monkeypatch.setenv('GITHUB_INSTALLATION_ID', '67890')
    monkeypatch.setenv('GITHUB_PRIVATE_KEY_PATH', 'test-key.pem')
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
    monkeypatch.setenv('GITHUB_WEBHOOK_SECRET', 'test-secret')


@pytest.fixture
def mock_private_key_file(tmp_path):
    """Create a temporary private key file."""
    key_file = tmp_path / "test-key.pem"
    key_content = """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF8FqH2XZKbNj8xSNiLnmvlYnxF0s
-----END RSA PRIVATE KEY-----"""
    key_file.write_text(key_content)
    return str(key_file)


class TestGitHubClient:
    def test_init_with_env_vars(self, mock_env_vars):
        """Test GitHubClient initialization with environment variables."""
        client = GitHubClient()

        assert client.app_id == '12345'
        assert client.installation_id == '67890'
        assert client.private_key_path == 'test-key.pem'

    def test_init_missing_config(self):
        """Test GitHubClient initialization with missing configuration."""
        with patch.dict('os.environ', {}, clear=True):
            client = GitHubClient()
            assert client.app_id is None
            assert client.installation_id is None

    @patch('builtins.open', create=True)
    def test_get_private_key_from_file(self, mock_open, mock_env_vars):
        """Test retrieving private key from file."""
        mock_file = MagicMock()
        mock_file.read.return_value = "FAKE_PRIVATE_KEY"
        mock_open.return_value.__enter__.return_value = mock_file

        client = GitHubClient()
        key = client._get_private_key()

        assert key == "FAKE_PRIVATE_KEY"
        mock_open.assert_called_once_with('test-key.pem', 'r')

    def test_get_private_key_from_env_var(self, monkeypatch):
        """Test retrieving private key from environment variable."""
        monkeypatch.setenv('GITHUB_APP_ID', '12345')
        monkeypatch.setenv('GITHUB_INSTALLATION_ID', '67890')
        monkeypatch.setenv('GITHUB_PRIVATE_KEY', 'SECRET_KEY_FROM_ENV')
        monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
        monkeypatch.setenv('GITHUB_WEBHOOK_SECRET', 'test-secret')

        client = GitHubClient()
        key = client._get_private_key()

        assert key == "SECRET_KEY_FROM_ENV"

    @patch('app.clients.github_client.jwt.encode')
    @patch.object(GitHubClient, '_get_private_key')
    def test_generate_jwt(self, mock_get_key, mock_jwt_encode, mock_env_vars):
        """Test JWT generation."""
        mock_get_key.return_value = "FAKE_KEY"
        mock_jwt_encode.return_value = "FAKE_JWT_TOKEN"

        client = GitHubClient()
        token = client._generate_jwt()

        assert token == "FAKE_JWT_TOKEN"
        mock_jwt_encode.assert_called_once()

    @patch('app.clients.github_client.requests.post')
    @patch.object(GitHubClient, '_generate_jwt')
    def test_get_installation_access_token(self, mock_jwt, mock_post, mock_env_vars):
        """Test getting installation access token."""
        mock_jwt.return_value = "JWT_TOKEN"
        mock_response = MagicMock()
        mock_response.json.return_value = {'token': 'INSTALL_TOKEN'}
        mock_post.return_value = mock_response

        client = GitHubClient()
        token = client.get_installation_access_token()

        assert token == 'INSTALL_TOKEN'
        mock_post.assert_called_once()

    @patch('app.clients.github_client.requests.post')
    @patch.object(GitHubClient, 'get_installation_access_token')
    def test_get_registration_token_for_repo(self, mock_install_token, mock_post, mock_env_vars):
        """Test getting registration token for a repository."""
        mock_install_token.return_value = "INSTALL_TOKEN"
        mock_response = MagicMock()
        mock_response.json.return_value = {'token': 'REG_TOKEN'}
        mock_post.return_value = mock_response

        client = GitHubClient()
        token = client.get_registration_token(repo_name='owner/repo')

        assert token == 'REG_TOKEN'
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert 'repos/owner/repo' in args[0]

    @patch('app.clients.github_client.requests.post')
    @patch.object(GitHubClient, 'get_installation_access_token')
    def test_get_registration_token_for_org(self, mock_install_token, mock_post, mock_env_vars):
        """Test getting registration token for an organization."""
        mock_install_token.return_value = "INSTALL_TOKEN"
        mock_response = MagicMock()
        mock_response.json.return_value = {'token': 'ORG_TOKEN'}
        mock_post.return_value = mock_response

        client = GitHubClient()
        token = client.get_registration_token(org_name='my-org')

        assert token == 'ORG_TOKEN'
        args, kwargs = mock_post.call_args
        assert 'orgs/my-org' in args[0]

    @patch.object(GitHubClient, 'get_installation_access_token')
    def test_get_registration_token_no_params(self, mock_install_token, mock_env_vars):
        """Test that ValueError is raised when neither org nor repo is provided."""
        mock_install_token.return_value = "FAKE_TOKEN"
        client = GitHubClient()

        with pytest.raises(ValueError, match="Either org_name or repo_name must be provided"):
            client.get_registration_token()

    def test_get_private_key_no_source(self):
        """Test that ValueError is raised when no private key source is configured."""
        with patch.dict('os.environ', {'GITHUB_APP_ID': '12345', 'GITHUB_INSTALLATION_ID': '67890'}, clear=True):
            client = GitHubClient()
            with pytest.raises(ValueError, match="No private key source configured"):
                client._get_private_key()

    @patch.object(GitHubClient, '_get_private_key')
    @patch('app.clients.github_client.jwt.encode')
    def test_generate_jwt_error(self, mock_jwt, mock_get_key, mock_env_vars):
        """Test JWT generation error handling."""
        mock_get_key.side_effect = Exception("Key error")

        client = GitHubClient()
        with pytest.raises(Exception, match="Key error"):
            client._generate_jwt()


class TestGitHubClientDeliveryIdLogging:
    """Tests to verify that delivery_id is logged in GitHubClient methods."""

    @patch("app.clients.github_client.requests.post")
    @patch.object(GitHubClient, "get_installation_access_token")
    def test_registration_token_for_repo_logs_delivery_id(
        self, mock_install_token, mock_post, mock_env_vars, caplog
    ):
        """Test that delivery_id is logged when getting a registration token for a repo."""
        mock_install_token.return_value = "INSTALL_TOKEN"
        mock_response = MagicMock()
        mock_response.json.return_value = {"token": "REG_TOKEN"}
        mock_post.return_value = mock_response

        client = GitHubClient()

        with caplog.at_level(logging.INFO, logger="app.clients.github_client"):
            client.get_registration_token(
                repo_name="owner/repo", delivery_id="gh-repo-delivery-001"
            )

        assert any(
            "gh-repo-delivery-001" in r.message for r in caplog.records
        ), "delivery_id not found in log for repo registration token"

    @patch("app.clients.github_client.requests.post")
    @patch.object(GitHubClient, "get_installation_access_token")
    def test_registration_token_for_org_logs_delivery_id(
        self, mock_install_token, mock_post, mock_env_vars, caplog
    ):
        """Test that delivery_id is logged when getting a registration token for an org."""
        mock_install_token.return_value = "INSTALL_TOKEN"
        mock_response = MagicMock()
        mock_response.json.return_value = {"token": "ORG_TOKEN"}
        mock_post.return_value = mock_response

        client = GitHubClient()

        with caplog.at_level(logging.INFO, logger="app.clients.github_client"):
            client.get_registration_token(
                org_name="my-org", delivery_id="gh-org-delivery-001"
            )

        assert any(
            "gh-org-delivery-001" in r.message for r in caplog.records
        ), "delivery_id not found in log for org registration token"


def _response(status_code=200, json_body=None, next_url=None):
    response = MagicMock(status_code=status_code)
    response.json.return_value = json_body
    response.links = {'next': {'url': next_url}} if next_url else {}
    return response


class TestRunnerAndDeliveryApi:
    @pytest.fixture(autouse=True)
    def tokens(self, mock_env_vars):
        with patch.object(GitHubClient, '_generate_jwt', return_value='jwt'), \
                patch.object(GitHubClient, '_fetch_installation_access_token', return_value='install'):
            yield

    @patch('app.clients.github_client.requests.post')
    def test_registration_token_is_reused_within_its_lifetime(self, mock_post):
        mock_post.return_value = _response(json_body={'token': 'reg'})
        client = GitHubClient()
        assert [client.get_registration_token(org_name='acme') for _ in range(3)] == ['reg'] * 3
        mock_post.assert_called_once()

    @patch('app.clients.github_client.requests.get')
    def test_org_runners_are_listed_across_pages(self, mock_get):
        mock_get.side_effect = [
            _response(json_body={'runners': [{'name': 'a'}]}, next_url='https://api.github.com/next'),
            _response(json_body={'runners': [{'name': 'b'}]}),
        ]
        assert [r['name'] for r in GitHubClient().list_org_runners('acme')] == ['a', 'b']

    @pytest.mark.parametrize('status_code, deleted', [(204, True), (404, True), (422, False)])
    @patch('app.clients.github_client.requests.delete')
    def test_deregistering_a_runner_reports_whether_it_is_gone(self, mock_delete, status_code, deleted):
        mock_delete.return_value = _response(status_code)
        assert GitHubClient().delete_org_runner('acme', 7) is deleted

    @patch('app.clients.github_client.requests.get')
    def test_failed_deliveries_stop_at_the_lookback(self, mock_get):
        mock_get.side_effect = [
            _response(json_body=[
                {'id': 2, 'delivered_at': '2026-11-02T14:29:00Z'}, {'id': 1, 'delivered_at': '2026-11-02T14:00:00Z'},
            ], next_url='https://api.github.com/next'),
        ]
        since = datetime(2026, 11, 2, 14, 10, tzinfo=timezone.utc)
        assert [d['id'] for d in GitHubClient().list_failed_deliveries(since)] == [2]
        assert mock_get.call_args.kwargs['params'] == {'per_page': 100, 'status': 'failure'}
        mock_get.assert_called_once()

    @patch('app.clients.github_client.requests.post')
    def test_redelivery_posts_a_new_attempt(self, mock_post):
        mock_post.return_value = _response(202)
        GitHubClient().redeliver(99)
        assert mock_post.call_args.args[0] == 'https://api.github.com/app/hook/deliveries/99/attempts'
