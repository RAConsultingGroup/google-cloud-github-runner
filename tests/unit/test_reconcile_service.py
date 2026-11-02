import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
import pytest
from google.api_core.exceptions import NotFound
from app.clients.gcloud_client import RunnerInstance
from app.services.reconcile_service import ReconcileService

LABEL = 'gcp-ubuntu-24-04-4core'
# A Monday, 14:30 UTC
NOW = datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)


def instance(name, minutes_old, label=LABEL):
    return RunnerInstance(name, 'europe-west10-a', label, NOW - timedelta(minutes=minutes_old), 'RUNNING')


def runner(name, status='online', busy=False, runner_id=None):
    return {'id': runner_id or hash(name) % 10000, 'name': name, 'status': status, 'busy': busy}


def delivery(delivery_id, guid, minutes_ago, event='workflow_job'):
    return {'id': delivery_id, 'guid': guid, 'event': event,
            'delivered_at': (NOW - timedelta(minutes=minutes_ago)).isoformat()}


@pytest.fixture
def clients(monkeypatch):
    monkeypatch.setenv('GITHUB_RUNNER_ORG', 'acme')
    monkeypatch.setenv('WARM_POOL_SCHEDULE', json.dumps({LABEL: [0] * 14 + [2] + [0] * 9}))
    with patch('app.services.reconcile_service.GitHubClient') as github_class, \
            patch('app.services.reconcile_service.GCloudClient') as gcloud_class:
        github, gcloud = Mock(), Mock()
        github.list_failed_deliveries.return_value = []
        github.list_org_runners.return_value = []
        github.find_org_runner.return_value = None
        github.delete_org_runner.return_value = True
        github.get_registration_token.return_value = 'reg-token'
        gcloud.list_runner_instances.return_value = []
        github_class.return_value, gcloud_class.return_value = github, gcloud
        yield github, gcloud


def deleted_vms(gcloud):
    return {c.args[0] for c in gcloud.delete_runner_instance.call_args_list}


class TestRedelivery:
    def test_recently_failed_job_webhook_is_redelivered_once_per_guid(self, clients):
        github, _ = clients
        github.list_failed_deliveries.return_value = [
            delivery(1, 'job-a', minutes_ago=4), delivery(2, 'job-a', minutes_ago=1), delivery(3, 'job-b', minutes_ago=2),
        ]
        assert ReconcileService(now=NOW).run()['redelivered'] == 2
        assert sorted(c.args[0] for c in github.redeliver.call_args_list) == [2, 3]

    def test_delivery_whose_last_failure_is_old_is_not_retried(self, clients):
        github, _ = clients
        github.list_failed_deliveries.return_value = [delivery(1, 'job-a', minutes_ago=11)]
        ReconcileService(now=NOW).run()
        github.redeliver.assert_not_called()

    def test_failures_from_several_missed_runs_ago_are_still_retried(self, clients):
        github, _ = clients
        github.list_failed_deliveries.return_value = [delivery(1, 'job-a', minutes_ago=8)]
        assert ReconcileService(now=NOW).run()['redelivered'] == 1

    def test_one_run_redelivers_a_bounded_number_of_webhooks(self, clients):
        github, _ = clients
        github.list_failed_deliveries.return_value = [delivery(i, f'job-{i}', minutes_ago=1) for i in range(150)]
        assert ReconcileService(now=NOW).run()['redelivered'] == 100

    def test_non_job_webhooks_are_not_retried(self, clients):
        github, _ = clients
        github.list_failed_deliveries.return_value = [delivery(1, 'ping', minutes_ago=1, event='ping')]
        ReconcileService(now=NOW).run()
        github.redeliver.assert_not_called()

    def test_failed_redelivery_does_not_stop_the_run(self, clients):
        github, gcloud = clients
        github.list_failed_deliveries.return_value = [delivery(1, 'a', 1), delivery(2, 'b', 1)]
        github.redeliver.side_effect = [RuntimeError('boom'), None]
        assert ReconcileService(now=NOW).run()['redelivered'] == 1
        gcloud.list_runner_instances.assert_called_once()


class TestCleanup:
    def test_vm_without_online_runner_is_deleted_after_boot_grace(self, clients):
        github, gcloud = clients
        gcloud.list_runner_instances.return_value = [instance('never-registered', 11), instance('booting', 2)]
        ReconcileService(now=NOW).run()
        assert deleted_vms(gcloud) == {'never-registered'}

    def test_vm_whose_runner_the_paged_listing_missed_is_kept(self, clients):
        github, gcloud = clients
        gcloud.list_runner_instances.return_value = [instance('busy-but-unlisted', 30)]
        github.find_org_runner.return_value = runner('busy-but-unlisted', busy=True)
        ReconcileService(now=NOW).run()
        github.find_org_runner.assert_called_once_with('acme', 'busy-but-unlisted')
        assert deleted_vms(gcloud) == set()

    def test_one_run_deletes_a_bounded_number_of_vms(self, clients):
        _, gcloud = clients
        gcloud.list_runner_instances.return_value = [instance(f'orphan-{i}', 30) for i in range(60)]
        assert ReconcileService(now=NOW).run()['deleted'] == 40

    def test_offline_runner_is_deregistered_and_its_vm_deleted(self, clients):
        github, gcloud = clients
        gcloud.list_runner_instances.return_value = [instance('preempted', 30)]
        github.list_org_runners.return_value = [runner('preempted', status='offline', runner_id=7)]
        ReconcileService(now=NOW).run()
        github.delete_org_runner.assert_called_once_with('acme', 7)
        assert deleted_vms(gcloud) == {'preempted'}

    def test_busy_runner_is_never_deleted(self, clients):
        github, gcloud = clients
        gcloud.list_runner_instances.return_value = [instance('working', 200)]
        github.list_org_runners.return_value = [runner('working', busy=True)]
        ReconcileService(now=NOW).run()
        assert deleted_vms(gcloud) == set()

    def test_idle_spares_beyond_the_pool_are_deleted_keeping_the_youngest(self, clients):
        github, gcloud = clients
        names = {'fresh': 2, 'young': 10, 'mid': 20, 'old': 40}
        gcloud.list_runner_instances.return_value = [instance(n, m) for n, m in names.items()]
        github.list_org_runners.return_value = [runner(n) for n in names]
        ReconcileService(now=NOW).run()
        # Pool target at 14:00 UTC is 2: 'young' and 'mid' stay, 'fresh' is still within its own job's grace.
        assert deleted_vms(gcloud) == {'old'}

    def test_idle_runner_near_max_run_duration_is_replaced_even_within_the_pool(self, clients):
        github, gcloud = clients
        gcloud.list_runner_instances.return_value = [instance('ancient', 190)]
        github.list_org_runners.return_value = [runner('ancient')]
        result = ReconcileService(now=NOW).run()
        assert deleted_vms(gcloud) == {'ancient'}
        assert result['created'] == 2

    def test_runner_that_picks_up_a_job_during_deregistration_keeps_its_vm(self, clients):
        github, gcloud = clients
        gcloud.list_runner_instances.return_value = [instance('spare', 40)]
        github.list_org_runners.return_value = [runner('spare')]
        github.delete_org_runner.return_value = False
        ReconcileService(now=NOW + timedelta(hours=3)).run()
        gcloud.delete_runner_instance.assert_not_called()

    def test_vm_already_gone_counts_as_deleted(self, clients):
        _, gcloud = clients
        gcloud.list_runner_instances.return_value = [instance('gone', 30)]
        gcloud.delete_runner_instance.side_effect = NotFound('gone')
        assert ReconcileService(now=NOW).run()['deleted'] == 1


class TestWarmPool:
    def test_pool_is_topped_up_to_the_scheduled_size_counting_idle_and_booting_runners(self, clients):
        github, gcloud = clients
        gcloud.list_runner_instances.return_value = [instance('idle', 1), instance('booting', 1), instance('busy', 1)]
        github.list_org_runners.return_value = [runner('idle'), runner('busy', busy=True)]
        with patch('app.services.reconcile_service.ReconcileService.pool_targets', return_value={LABEL: 5}):
            assert ReconcileService(now=NOW).run()['created'] == 3
        gcloud.create_runner_instance.assert_called_with('reg-token', 'https://github.com/acme', LABEL, 'acme/warm-pool')

    def test_no_pool_outside_scheduled_hours_or_on_weekends(self, clients):
        _, gcloud = clients
        ReconcileService(now=NOW.replace(hour=20)).run()
        ReconcileService(now=NOW + timedelta(days=5)).run()
        gcloud.create_runner_instance.assert_not_called()

    def test_quota_error_stops_the_top_up_until_the_next_run(self, clients):
        _, gcloud = clients
        gcloud.create_runner_instance.side_effect = RuntimeError('QUOTA_EXCEEDED')
        assert ReconcileService(now=NOW).run()['created'] == 0
        assert gcloud.create_runner_instance.call_count == 1

    def test_schedule_must_cover_every_hour(self, clients, monkeypatch):
        monkeypatch.setenv('WARM_POOL_SCHEDULE', json.dumps({LABEL: [1, 2]}))
        with pytest.raises(ValueError, match='24 hourly sizes'):
            ReconcileService(now=NOW)
