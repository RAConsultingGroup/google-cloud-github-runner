"""
Periodic reconciliation of runner VMs with GitHub, driven by Cloud Scheduler.

Webhooks alone lose jobs: GitHub never retries a failed delivery, so a queued-job webhook that hit a
GCE quota error or a saturated manager leaves its job without a VM. Each run retries those
deliveries, keeps a scheduled pool of idle runners ready, and deletes runner VMs nothing will use.
"""
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from google.api_core.exceptions import NotFound
from app.clients import GitHubClient, GCloudClient

logger = logging.getLogger(__name__)

# Failed deliveries are retried once per run; the lookback bounds how long a job keeps being retried.
DELIVERY_LOOKBACK = timedelta(minutes=20)
# Retry a delivery only if its latest failure is recent, i.e. it was not already retried successfully.
# Wide enough to survive several failed reconcile runs during a burst; a duplicate is harmless because
# job VMs are named after the job.
RETRY_WINDOW = timedelta(minutes=10)
# Bounds on one run's work, so a run stays well inside the scheduler's attempt deadline; the next run
# continues where this one stopped.
MAX_REDELIVERIES_PER_RUN = 100
MAX_DELETES_PER_RUN = 40
# A VM whose runner is not online this long after creation failed to boot, or already ran its job.
BOOT_GRACE = timedelta(minutes=10)
# An idle runner this old was not wanted by the job it was created for: it is a spare.
SPARE_AGE = timedelta(minutes=5)
# Pool runners are replaced before the VMs' max_run_duration (4h) could cut a job short.
MAX_IDLE_AGE = timedelta(hours=3)


class ReconcileService:
    """Retries failed webhooks, keeps the warm pool at its scheduled size and deletes unneeded runner VMs."""

    def __init__(self, now=None):
        self.github_client = GitHubClient()
        self.gcloud_client = GCloudClient()
        self.org_name = os.environ['GITHUB_RUNNER_ORG']
        self.warm_pool = json.loads(os.environ.get('WARM_POOL_SCHEDULE') or '{}')
        for label, sizes in self.warm_pool.items():
            if len(sizes) != 24:
                raise ValueError(f"WARM_POOL_SCHEDULE[{label!r}] needs 24 hourly sizes (UTC), got {len(sizes)}")
        self.now = now or datetime.now(timezone.utc)

    def run(self):
        """Run one reconciliation pass and return what it did."""
        redelivered = self.redeliver_failed_webhooks()
        runners = {runner['name']: runner for runner in self.github_client.list_org_runners(self.org_name)}
        instances = self.gcloud_client.list_runner_instances()
        deleted = self.delete_unneeded(instances, runners)
        remaining = [instance for instance in instances if instance.name not in deleted]
        created = self.top_up_warm_pool(remaining, runners)
        result = {'redelivered': redelivered, 'deleted': len(deleted), 'created': created}
        logger.info("Reconcile finished: %s", result)
        return result

    def pool_targets(self):
        """Warm pool size per runner label for the current hour: the schedule on weekdays, none on weekends."""
        if self.now.weekday() >= 5:
            return {}
        return {label: sizes[self.now.hour] for label, sizes in self.warm_pool.items()}

    def redeliver_failed_webhooks(self):
        """Redeliver workflow_job webhooks whose latest delivery failed recently."""
        attempts = defaultdict(list)
        for delivery in self.github_client.list_failed_deliveries(since=self.now - DELIVERY_LOOKBACK):
            if delivery['event'] == 'workflow_job':
                attempts[delivery['guid']].append(delivery)
        redelivered = 0
        for deliveries in attempts.values():
            latest = max(deliveries, key=lambda delivery: delivery['delivered_at'])
            if self.now - datetime.fromisoformat(latest['delivered_at']) > RETRY_WINDOW:
                continue
            if redelivered >= MAX_REDELIVERIES_PER_RUN:
                break
            try:
                self.github_client.redeliver(latest['id'])
                redelivered += 1
            except Exception as e:
                logger.error("Failed to redeliver webhook delivery %s (guid %s): %s", latest['id'], latest['guid'], e)
        return redelivered

    def delete_unneeded(self, instances, runners):
        """Delete broken or finished runner VMs, and idle spares beyond the warm pool. Returns deleted names."""
        deleted = set()
        for instance in instances:
            if len(deleted) >= MAX_DELETES_PER_RUN:
                return deleted
            runner = runners.get(instance.name)
            if runner is None and self._age(instance) > BOOT_GRACE:
                # The paged listing can miss a runner when others deregister mid-listing; never delete
                # a VM on that alone.
                runner = self.github_client.find_org_runner(self.org_name, instance.name)
            if (runner is None or runner['status'] != 'online') and self._age(instance) > BOOT_GRACE:
                if self._delete(instance, runner):
                    deleted.add(instance.name)
        kept = defaultdict(int)
        targets = self.pool_targets()
        idle = [instance for instance in instances if self._is_idle(instance, runners)]
        for instance in sorted(idle, key=lambda instance: instance.created_at, reverse=True):
            age = self._age(instance)
            if age < SPARE_AGE:
                continue
            if age < MAX_IDLE_AGE and kept[instance.runner_label] < targets.get(instance.runner_label, 0):
                kept[instance.runner_label] += 1
                continue
            if len(deleted) >= MAX_DELETES_PER_RUN:
                break
            if self._delete(instance, runners[instance.name]):
                deleted.add(instance.name)
        return deleted

    def top_up_warm_pool(self, instances, runners):
        """Create pool VMs until each label has its scheduled number of idle or booting runners."""
        created = 0
        for label, target in self.pool_targets().items():
            ready = sum(
                1 for instance in instances
                if instance.runner_label == label
                and (self._is_idle(instance, runners) or (instance.name not in runners and self._age(instance) < BOOT_GRACE))
            )
            for _ in range(target - ready):
                try:
                    token = self.github_client.get_registration_token(org_name=self.org_name)
                    self.gcloud_client.create_runner_instance(
                        token, f"https://github.com/{self.org_name}", label, f"{self.org_name}/warm-pool"
                    )
                    created += 1
                except Exception as e:
                    logger.error("Failed to create warm pool runner for %s, retrying next run: %s", label, e)
                    break
        return created

    def _age(self, instance):
        return self.now - instance.created_at

    @staticmethod
    def _is_idle(instance, runners):
        runner = runners.get(instance.name)
        return runner is not None and runner['status'] == 'online' and not runner['busy']

    def _delete(self, instance, runner):
        """Deregister the runner (refused while it runs a job), then delete its VM. Returns whether it was deleted."""
        try:
            if runner is not None and not self.github_client.delete_org_runner(self.org_name, runner['id']):
                return False
            self.gcloud_client.delete_runner_instance(instance.name, zone=instance.zone)
            return True
        except NotFound:
            return True
        except Exception as e:
            logger.error("Failed to delete runner VM %s in %s: %s", instance.name, instance.zone, e)
            return False
