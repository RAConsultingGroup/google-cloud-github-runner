import pytest
import logging
from unittest.mock import patch, MagicMock
from app.clients.gcloud_client import GCloudClient


@pytest.fixture
def mock_env_vars(monkeypatch):
    """Set up mock environment variables for GCloud client."""
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
    monkeypatch.setenv('GOOGLE_CLOUD_ZONE', 'us-central1-a')
    monkeypatch.delenv('GOOGLE_CLOUD_ZONES', raising=False)


@pytest.fixture
def mock_compute_clients():
    """Mock the Compute Engine clients."""
    with patch('app.clients.gcloud_client.compute_v1.InstancesClient') as mock_instances, \
         patch('app.clients.gcloud_client.compute_v1.RegionInstanceTemplatesClient') as mock_templates:
        yield mock_instances, mock_templates


@pytest.fixture
def mock_gcloud_auth():
    """Mock Google Cloud authentication to prevent credential errors."""
    with patch('google.auth.default', return_value=(MagicMock(), 'test-project')):
        yield


class TestGCloudClient:
    def test_init_with_env_vars(self, mock_env_vars, mock_compute_clients, mock_gcloud_auth):
        """Test GCloudClient initialization with environment variables."""
        client = GCloudClient()

        assert client.project_id == 'test-project'
        assert client.zone == 'us-central1-a'
        assert client.github_runner_group == ''
        assert client.region == 'us-central1'

    def test_init_default_zone(self, monkeypatch, mock_compute_clients, mock_gcloud_auth):
        """Test GCloudClient initialization with default zone."""
        monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
        monkeypatch.delenv('GOOGLE_CLOUD_ZONE', raising=False)

        client = GCloudClient()

        assert client.zone == 'us-central1-a'
        assert client.region == 'us-central1'

    def test_init_with_runner_group(self, monkeypatch, mock_compute_clients, mock_gcloud_auth):
        """Test GCloudClient initialization with runner group."""
        monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
        monkeypatch.setenv('GOOGLE_CLOUD_ZONE', 'us-central1-a')
        monkeypatch.setenv('GITHUB_RUNNER_GROUP', 'platform-runners')

        client = GCloudClient()

        assert client.github_runner_group == 'platform-runners'

    def test_init_missing_project_id(self, mock_compute_clients, mock_gcloud_auth):
        """Test GCloudClient initialization with missing project ID."""
        with patch.dict('os.environ', {}, clear=True):
            client = GCloudClient()
            assert client.project_id is None

    @patch('app.clients.gcloud_client.compute_v1')
    def test_create_runner_instance(self, mock_compute, mock_env_vars):
        """Test creating a runner instance."""
        mock_instance_client = MagicMock()
        mock_operation = MagicMock()
        mock_operation.name = 'operation-123'
        mock_instance_client.insert.return_value = mock_operation
        mock_compute.InstancesClient.return_value = mock_instance_client

        # Mock RegionInstanceTemplatesClient
        mock_templates_client = MagicMock()
        mock_template = MagicMock()
        mock_template.name = 'gcp-ubuntu-24-04-12345678901234'
        mock_template.self_link = ('https://www.googleapis.com/compute/v1/projects/test-project/regions/us-central1/'
                                   'instanceTemplates/gcp-ubuntu-24-04-12345678901234')
        mock_templates_client.list.return_value = [mock_template]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()
        instance_name = client.create_runner_instance(
            'fake-token-12345678',
            'https://github.com/owner/repo',
            'gcp-ubuntu-24.04'
        )

        assert instance_name.startswith('gcp-runner-')
        mock_instance_client.insert.assert_called_once()

        startup_script = mock_compute.Items.call_args_list[0].kwargs['value']
        assert startup_script.startswith('cd /actions-runner && ')
        assert 'sudo -u runner ./config.sh' in startup_script
        assert 'sudo -u runner ./run.sh' in startup_script
        assert '--runnergroup' not in startup_script

    @patch('app.clients.gcloud_client.compute_v1')
    def test_create_runner_instance_with_runner_group(self, mock_compute, monkeypatch, mock_env_vars):
        """Test creating a runner instance with runner group."""
        monkeypatch.setenv('GITHUB_RUNNER_GROUP', 'platform-runners')

        mock_instance_client = MagicMock()
        mock_operation = MagicMock()
        mock_operation.name = 'operation-123'
        mock_instance_client.insert.return_value = mock_operation
        mock_compute.InstancesClient.return_value = mock_instance_client

        mock_templates_client = MagicMock()
        mock_template = MagicMock()
        mock_template.name = 'gcp-ubuntu-24-04-12345678901234'
        mock_template.self_link = ('https://www.googleapis.com/compute/v1/projects/test-project/regions/us-central1/'
                                   'instanceTemplates/gcp-ubuntu-24-04-12345678901234')
        mock_templates_client.list.return_value = [mock_template]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()
        client.create_runner_instance(
            'fake-token-12345678',
            'https://github.com/owner/repo',
            'gcp-ubuntu-24.04'
        )

        startup_script = mock_compute.Items.call_args_list[0].kwargs['value']
        assert startup_script.startswith('cd /actions-runner && ')
        assert '--runnergroup platform-runners' in startup_script

    @patch('app.clients.gcloud_client.compute_v1')
    def test_create_runner_instance_error(self, mock_compute, mock_env_vars):
        """Test error handling when creating instance fails."""
        mock_instance_client = MagicMock()
        mock_instance_client.insert.side_effect = Exception("API Error")
        mock_compute.InstancesClient.return_value = mock_instance_client

        # Mock RegionInstanceTemplatesClient
        mock_templates_client = MagicMock()
        mock_template = MagicMock()
        mock_template.name = 'gcp-ubuntu-24-04-12345678901234'
        mock_template.self_link = ('https://www.googleapis.com/compute/v1/projects/test-project/regions/us-central1/'
                                   'instanceTemplates/gcp-ubuntu-24-04-12345678901234')
        mock_templates_client.list.return_value = [mock_template]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()

        with pytest.raises(Exception, match="API Error"):
            client.create_runner_instance(
                'fake-token',
                'https://github.com/owner/repo',
                'gcp-ubuntu-24.04'
            )

    @patch('app.clients.gcloud_client.compute_v1')
    def test_delete_runner_instance(self, mock_compute, mock_env_vars):
        """Test deleting a runner instance."""
        mock_instance_client = MagicMock()
        mock_operation = MagicMock()
        mock_operation.name = 'delete-operation-123'
        mock_instance_client.delete.return_value = mock_operation
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()
        client.delete_runner_instance('runner-12345')

        mock_instance_client.delete.assert_called_once_with(
            project='test-project',
            zone='us-central1-a',
            instance='runner-12345'
        )

    @patch('app.clients.gcloud_client.compute_v1')
    def test_delete_runner_instance_error(self, mock_compute, mock_env_vars):
        """Test error handling when deleting instance fails."""
        mock_instance_client = MagicMock()
        mock_instance_client.delete.side_effect = Exception("Delete Error")
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()

        with pytest.raises(Exception, match="Delete Error"):
            client.delete_runner_instance('runner-12345')

    @patch('app.clients.gcloud_client.compute_v1')
    def test_get_template_name_found(self, mock_compute, mock_env_vars):
        """Test finding a template by prefix."""
        mock_templates_client = MagicMock()
        mock_template1 = MagicMock()
        mock_template1.name = 'other-template'
        mock_template2 = MagicMock()
        mock_template2.name = 'gcp-target-template-123456789012345'

        mock_templates_client.list.return_value = [mock_template1, mock_template2]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()
        result = client._get_template_name('gcp-target-template')

        assert result.name == 'gcp-target-template-123456789012345'

    @patch('app.clients.gcloud_client.compute_v1')
    def test_get_template_name_not_found(self, mock_compute, mock_env_vars):
        """Test not finding a template."""
        mock_templates_client = MagicMock()
        mock_template = MagicMock()
        mock_template.name = 'other-template'

        mock_templates_client.list.return_value = [mock_template]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()
        result = client._get_template_name('non-existent')

        assert result is None

    @patch('app.clients.gcloud_client.compute_v1')
    def test_get_template_name_exception(self, mock_compute, mock_env_vars):
        """Test handling exception when getting template."""
        mock_templates_client = MagicMock()
        mock_templates_client.list.side_effect = Exception("API Error")
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()
        result = client._get_template_name('any-template')

        assert result is None

    @patch('app.clients.gcloud_client.compute_v1')
    def test_create_runner_instance_no_template(self, mock_compute, mock_env_vars):
        """Test creating runner instance when no template is found."""
        mock_templates_client = MagicMock()
        mock_templates_client.list.return_value = []
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        mock_instance_client = MagicMock()
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()
        result = client.create_runner_instance(
            'fake-token',
            'https://github.com/owner/repo',
            'non-existent-template'
        )

        assert result is None
        mock_instance_client.insert.assert_not_called()


class TestGCloudClientMultiZone:
    """Tests for spreading runner VMs across multiple zones via GOOGLE_CLOUD_ZONES."""

    @pytest.fixture
    def mock_multi_zone_env(self, monkeypatch):
        """Configure three zones via GOOGLE_CLOUD_ZONES."""
        monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
        monkeypatch.setenv('GOOGLE_CLOUD_ZONE', 'us-central1-a')
        monkeypatch.setenv('GOOGLE_CLOUD_ZONES', 'europe-west10-a, europe-west10-b,europe-west10-c')

    def _mock_template(self, mock_compute):
        """Wire a matching regional instance template into the mocked compute client."""
        mock_templates_client = MagicMock()
        mock_template = MagicMock()
        mock_template.name = 'gcp-ubuntu-24-04-12345678901234'
        mock_template.self_link = ('https://www.googleapis.com/compute/v1/projects/test-project/regions/europe-west10/'
                                   'instanceTemplates/gcp-ubuntu-24-04-12345678901234')
        mock_templates_client.list.return_value = [mock_template]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

    def test_init_parses_zone_list(self, mock_multi_zone_env, mock_compute_clients, mock_gcloud_auth):
        """GOOGLE_CLOUD_ZONES overrides the single zone and derives the region from its first entry."""
        client = GCloudClient()

        assert client.zones == ['europe-west10-a', 'europe-west10-b', 'europe-west10-c']
        assert client.region == 'europe-west10'

    def test_init_without_zone_list_keeps_single_zone(self, mock_env_vars, mock_compute_clients, mock_gcloud_auth):
        """Without GOOGLE_CLOUD_ZONES the client behaves exactly as before."""
        client = GCloudClient()

        assert client.zones == ['us-central1-a']
        assert client.region == 'us-central1'

    def test_init_warns_on_cross_region_zone(self, monkeypatch, mock_compute_clients, mock_gcloud_auth, caplog):
        """A zone outside the first zone's region is flagged, since templates are regional."""
        monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
        monkeypatch.setenv('GOOGLE_CLOUD_ZONES', 'europe-west10-a,europe-west6-b')

        with caplog.at_level(logging.WARNING, logger='app.clients.gcloud_client'):
            GCloudClient()

        assert any('europe-west6-b' in r.message for r in caplog.records)

    @patch('app.clients.gcloud_client.compute_v1')
    def test_create_runner_instance_uses_picked_zone(self, mock_compute, mock_multi_zone_env):
        """The instance is inserted into the zone picked from the configured list."""
        self._mock_template(mock_compute)
        mock_instance_client = MagicMock()
        mock_compute.InstancesClient.return_value = mock_instance_client

        with patch('app.clients.gcloud_client.random.choice', return_value='europe-west10-b') as mock_choice:
            client = GCloudClient()
            client.create_runner_instance(
                'fake-token-12345678',
                'https://github.com/owner/repo',
                'gcp-ubuntu-24.04'
            )

        mock_choice.assert_called_once_with(['europe-west10-a', 'europe-west10-b', 'europe-west10-c'])
        assert mock_compute.InsertInstanceRequest.call_args.kwargs['zone'] == 'europe-west10-b'

    @patch('app.clients.gcloud_client.compute_v1')
    def test_create_runner_instances_spread_across_zones(self, mock_compute, mock_multi_zone_env):
        """Repeated creations land in more than one of the configured zones."""
        self._mock_template(mock_compute)
        mock_instance_client = MagicMock()
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()
        for _ in range(50):
            client.create_runner_instance(
                'fake-token-12345678',
                'https://github.com/owner/repo',
                'gcp-ubuntu-24.04'
            )

        used_zones = {call.kwargs['zone'] for call in mock_compute.InsertInstanceRequest.call_args_list}
        assert used_zones <= {'europe-west10-a', 'europe-west10-b', 'europe-west10-c'}
        assert len(used_zones) > 1

    @patch('app.clients.gcloud_client.compute_v1')
    def test_delete_runner_instance_locates_zone(self, mock_compute, mock_multi_zone_env):
        """With multiple zones, the instance's actual zone is looked up before deletion."""
        mock_instance_client = MagicMock()
        mock_instance_client.aggregated_list.return_value = [
            ('zones/europe-west10-a', MagicMock(instances=[])),
            ('zones/europe-west10-c', MagicMock(instances=[MagicMock()])),
        ]
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()
        client.delete_runner_instance('gcp-runner-12345')

        mock_instance_client.delete.assert_called_once_with(
            project='test-project',
            zone='europe-west10-c',
            instance='gcp-runner-12345'
        )

    @patch('app.clients.gcloud_client.compute_v1')
    def test_delete_runner_instance_finds_vm_in_unconfigured_zone(self, mock_compute, mock_multi_zone_env):
        """A VM created before a zone/region change is still found and deleted."""
        mock_instance_client = MagicMock()
        mock_instance_client.aggregated_list.return_value = [
            ('zones/europe-west6-b', MagicMock(instances=[MagicMock()])),
        ]
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()
        client.delete_runner_instance('gcp-runner-12345')

        mock_instance_client.delete.assert_called_once_with(
            project='test-project',
            zone='europe-west6-b',
            instance='gcp-runner-12345'
        )

    @patch('app.clients.gcloud_client.compute_v1')
    def test_delete_runner_instance_missing_everywhere_skips(self, mock_compute, mock_multi_zone_env, caplog):
        """An already-deleted VM (e.g. after Spot preemption) is skipped without an error."""
        mock_instance_client = MagicMock()
        mock_instance_client.aggregated_list.return_value = [
            ('zones/europe-west10-a', MagicMock(instances=[])),
        ]
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()
        with caplog.at_level(logging.WARNING, logger='app.clients.gcloud_client'):
            client.delete_runner_instance('gcp-runner-12345', delivery_id='gce-missing-delivery-001')

        mock_instance_client.delete.assert_not_called()
        assert any('gce-missing-delivery-001' in r.message for r in caplog.records)

    @patch('app.clients.gcloud_client.compute_v1')
    def test_delete_runner_instance_single_zone_skips_lookup(self, mock_compute, mock_env_vars):
        """Without GOOGLE_CLOUD_ZONES deletion goes straight to the configured zone."""
        mock_instance_client = MagicMock()
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()
        client.delete_runner_instance('gcp-runner-12345')

        mock_instance_client.aggregated_list.assert_not_called()
        mock_instance_client.delete.assert_called_once_with(
            project='test-project',
            zone='us-central1-a',
            instance='gcp-runner-12345'
        )


class TestGCloudClientDeliveryIdLogging:
    """Tests to verify that delivery_id is logged in GCloudClient methods."""

    @patch("app.clients.gcloud_client.compute_v1")
    def test_create_runner_instance_logs_delivery_id(
        self, mock_compute, mock_env_vars, caplog
    ):
        """Test that delivery_id is logged when creating an instance."""
        mock_instance_client = MagicMock()
        mock_operation = MagicMock()
        mock_operation.name = "operation-123"
        mock_instance_client.insert.return_value = mock_operation
        mock_compute.InstancesClient.return_value = mock_instance_client

        mock_templates_client = MagicMock()
        mock_template = MagicMock()
        mock_template.name = "gcp-ubuntu-24-04-12345678901234"
        mock_template.self_link = (
            "https://www.googleapis.com/compute/v1/projects/test-project/regions/us-central1/"
            "instanceTemplates/gcp-ubuntu-24-04-12345678901234"
        )
        mock_templates_client.list.return_value = [mock_template]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()

        with caplog.at_level(logging.INFO, logger="app.clients.gcloud_client"):
            client.create_runner_instance(
                "fake-token",
                "https://github.com/owner/repo",
                "gcp-ubuntu-24.04",
                delivery_id="gce-create-delivery-001",
            )

        delivery_logs = [
            r for r in caplog.records if "gce-create-delivery-001" in r.message
        ]
        assert (
            len(delivery_logs) >= 2
        ), "Expected delivery_id in at least 2 log lines (template match + creating + operation)"

    @patch("app.clients.gcloud_client.compute_v1")
    def test_delete_runner_instance_logs_delivery_id(
        self, mock_compute, mock_env_vars, caplog
    ):
        """Test that delivery_id is logged when deleting an instance."""
        mock_instance_client = MagicMock()
        mock_operation = MagicMock()
        mock_operation.name = "delete-operation-123"
        mock_instance_client.delete.return_value = mock_operation
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()

        with caplog.at_level(logging.INFO, logger="app.clients.gcloud_client"):
            client.delete_runner_instance(
                "runner-12345", delivery_id="gce-delete-delivery-001"
            )

        delivery_logs = [
            r for r in caplog.records if "gce-delete-delivery-001" in r.message
        ]
        assert (
            len(delivery_logs) >= 2
        ), "Expected delivery_id in at least 2 log lines (deleting + operation)"

    @patch("app.clients.gcloud_client.compute_v1")
    def test_create_runner_instance_no_template_logs_delivery_id(
        self, mock_compute, mock_env_vars, caplog
    ):
        """Test that delivery_id is logged when no matching template found."""
        mock_templates_client = MagicMock()
        mock_templates_client.list.return_value = []
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        mock_instance_client = MagicMock()
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()

        with caplog.at_level(logging.WARNING, logger="app.clients.gcloud_client"):
            client.create_runner_instance(
                "fake-token",
                "https://github.com/owner/repo",
                "non-existent",
                delivery_id="gce-notemplate-delivery-001",
            )

        assert any(
            "gce-notemplate-delivery-001" in r.message for r in caplog.records
        ), "delivery_id not found in warning log for missing template"

    @patch("app.clients.gcloud_client.compute_v1")
    def test_create_runner_instance_error_logs_delivery_id(
        self, mock_compute, mock_env_vars, caplog
    ):
        """Test that delivery_id is logged when instance creation fails."""
        mock_instance_client = MagicMock()
        mock_instance_client.insert.side_effect = Exception("API Error")
        mock_compute.InstancesClient.return_value = mock_instance_client

        mock_templates_client = MagicMock()
        mock_template = MagicMock()
        mock_template.name = "gcp-ubuntu-24-04-12345678901234"
        mock_template.self_link = (
            "https://www.googleapis.com/compute/v1/projects/test-project/regions/us-central1/"
            "instanceTemplates/gcp-ubuntu-24-04-12345678901234"
        )
        mock_templates_client.list.return_value = [mock_template]
        mock_compute.RegionInstanceTemplatesClient.return_value = mock_templates_client

        client = GCloudClient()

        with caplog.at_level(logging.ERROR, logger="app.clients.gcloud_client"):
            with pytest.raises(Exception, match="API Error"):
                client.create_runner_instance(
                    "fake-token",
                    "https://github.com/owner/repo",
                    "gcp-ubuntu-24.04",
                    delivery_id="gce-error-delivery-001",
                )

        assert any(
            "gce-error-delivery-001" in r.message for r in caplog.records
        ), "delivery_id not found in error log on instance creation failure"

    @patch("app.clients.gcloud_client.compute_v1")
    def test_delete_runner_instance_error_logs_delivery_id(
        self, mock_compute, mock_env_vars, caplog
    ):
        """Test that delivery_id is logged when instance deletion fails."""
        mock_instance_client = MagicMock()
        mock_instance_client.delete.side_effect = Exception("Delete Error")
        mock_compute.InstancesClient.return_value = mock_instance_client

        client = GCloudClient()

        with caplog.at_level(logging.ERROR, logger="app.clients.gcloud_client"):
            with pytest.raises(Exception, match="Delete Error"):
                client.delete_runner_instance(
                    "runner-12345", delivery_id="gce-delerr-delivery-001"
                )

        assert any(
            "gce-delerr-delivery-001" in r.message for r in caplog.records
        ), "delivery_id not found in error log on instance deletion failure"
