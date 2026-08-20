"""
Google Cloud Client for managing GCE instances.
"""
import logging
import os
import random
import re
import uuid
import shlex
import google.cloud.compute_v1 as compute_v1

logger = logging.getLogger(__name__)


class GCloudClient:
    """Client for interacting with Google Cloud Compute Engine API."""

    def __init__(self):
        """Initialize GCloudClient with project and zone configuration."""
        self.project_id = os.environ.get('GOOGLE_CLOUD_PROJECT')
        self.zone = os.environ.get('GOOGLE_CLOUD_ZONE', 'us-central1-a')
        # GOOGLE_CLOUD_ZONES (comma-separated full zone names) spreads runner VMs across
        # multiple zones so a Spot capacity crunch in one zone only affects a fraction of
        # jobs. Unset it to keep the single-zone behavior of GOOGLE_CLOUD_ZONE.
        zones_env = os.environ.get('GOOGLE_CLOUD_ZONES', '')
        self.zones = [zone.strip() for zone in zones_env.split(',') if zone.strip()] or [self.zone]
        self.github_runner_group = os.environ.get('GITHUB_RUNNER_GROUP', '').strip()
        self.region = '-'.join(self.zones[0].split('-')[:-1])

        cross_region_zones = [zone for zone in self.zones if not zone.startswith(f"{self.region}-")]
        if cross_region_zones:
            # Instance templates are regional, so an insert into another region's zone fails.
            logger.warning(
                "GOOGLE_CLOUD_ZONES entries %s are outside region %s; instance creation in them will fail.",
                cross_region_zones,
                self.region,
            )

        if not self.project_id:
            logger.warning("GOOGLE_CLOUD_PROJECT not set. GCloudClient will not work correctly.")

        # https://docs.cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.services.instances.InstancesClient
        self.instance_client = compute_v1.InstancesClient()
        # Create a RegionInstanceTemplatesClient for retrieving templates in a specific region
        # https://docs.cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.services.region_instance_templates
        self.instance_templates_client = compute_v1.RegionInstanceTemplatesClient()

    def _get_template_name(self, template_name):
        """
        Find a matching instance template by name prefix.

        Args:
            template_name (str): The name prefix to search for.

        Returns:
            google.cloud.compute_v1.InstanceTemplate or None: The matching template resource.
        """
        # Replace dots with dashes for template name, so gcp-ubuntu-24.04 matches gcp-ubuntu-24-04
        prefix = template_name.replace('.', '-')
        # logger.info(f"Prefix: {prefix}")
        # Create regex pattern: prefix followed by dash, at least 12 digits, and optional alphanumeric characters
        pattern = re.compile(f"^{re.escape(prefix)}-\\d{{14,}}[a-z0-9]*$")
        try:
            # List all templates to find one that matches the pattern
            for template in self.instance_templates_client.list(project=self.project_id, region=self.region):
                # logger.info(f"Template: {template.name}")
                if pattern.match(template.name):
                    return template
            return None
        except Exception:
            return None

    def create_runner_instance(
        self,
        registration_token,
        repo_url,
        template_name,
        instance_label=None,
        delivery_id=None,
    ):
        """
        Create a new GCE instance for a GitHub Actions runner.

        Args:
            registration_token (str): The GitHub Actions runner registration token.
            repo_url (str): The URL of the repository or organization.
            template_name (str): The name of the instance template to use.
            instance_label (str): Label to add to the Instance for Cost Tracking.
            delivery_id (str): The GitHub webhook delivery ID for log correlation.

        Returns:
            str: The name of the created instance.
        """
        instance_template_resource = self._get_template_name(template_name)
        if instance_template_resource:
            logger.info(
                "Found matching instance template: %s, delivery_id: %s",
                instance_template_resource.name,
                delivery_id,
            )
        else:
            logger.warning(
                "No matching instance template found for label '%s' in region %s. "
                "Skipping instance creation. delivery_id: %s",
                template_name,
                self.region,
                delivery_id,
            )
            return None

        # Name must start with a lowercase letter followed by up to 62 lowercase letters,
        # numbers, or hyphens, and cannot end with a hyphen.
        instance_uuid = uuid.uuid4().hex[:16]
        if instance_template_resource.name.startswith("dependabot"):
            instance_name = f"gcp-runner-dependabot-{instance_uuid}"
        else:
            instance_name = f"gcp-runner-{instance_uuid}"

        zone = random.choice(self.zones)
        logger.info(
            "Creating GCE instance %s in zone %s with template %s, delivery_id: %s",
            instance_name,
            zone,
            instance_template_resource.self_link,
            delivery_id,
        )

        # Set instance name
        instance_resource = compute_v1.Instance()  # google.cloud.compute_v1.types.Instance
        instance_resource.name = instance_name

        if instance_label is not None:
            owner, repo = instance_label.split("/")
            instance_resource.labels = {
                "gha-owner": owner.lower(),
                "gha-repo": repo.lower(),
                "gha-runner": template_name
            }

        # Set metadata (startup script) - use shlex.quote to prevent command injection
        runner_group_flag = ""
        if self.github_runner_group:
            runner_group_flag = f" --runnergroup {shlex.quote(self.github_runner_group)}"

        startup_script = (
            "cd /actions-runner && "
            f"sudo -u runner ./config.sh --url {shlex.quote(repo_url)} "
            f"--token {shlex.quote(registration_token)} "
            f"--name {shlex.quote(instance_name)} "
            f"--labels {shlex.quote(template_name)} "
            f"{runner_group_flag} "
            "--ephemeral "
            "--unattended "
            "--no-default-labels "
            "--disableupdate && "
            "sudo -u runner ./run.sh"
        )
        metadata = compute_v1.Metadata()
        metadata.items = [
            compute_v1.Items(key="startup-script", value=startup_script),
            compute_v1.Items(key="vmDnsSetting", value="ZonalOnly"),
            compute_v1.Items(key="block-project-ssh-keys", value="true"),
        ]
        instance_resource.metadata = metadata

        # Create the request
        # https://docs.cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.types.InsertInstanceRequest
        request = compute_v1.InsertInstanceRequest(
            project=self.project_id,
            zone=zone,
            instance_resource=instance_resource,
            source_instance_template=instance_template_resource.self_link
        )

        try:
            # https://docs.cloud.google.com/compute/docs/reference/rest/v1/instances/insert
            operation = self.instance_client.insert(request=request)
            logger.info(
                "Instance creation operation started: %s, delivery_id: %s",
                operation.name,
                delivery_id,
            )
            return instance_name
        except Exception as e:
            logger.error(
                "Failed to create instance: %s, delivery_id: %s", e, delivery_id
            )
            raise

    def _find_instance_zone(self, instance_name):
        """
        Locate the zone of an instance via an aggregated list.

        Also finds instances in zones no longer configured (e.g. VMs created
        before a zone/region change that complete after it).

        Args:
            instance_name (str): The name of the instance to locate.

        Returns:
            str or None: The zone name, or None if the instance was not found.
        """
        request = compute_v1.AggregatedListInstancesRequest(
            project=self.project_id,
            filter=f'name = "{instance_name}"',
        )
        for zone_path, scoped_list in self.instance_client.aggregated_list(request=request):
            if getattr(scoped_list, 'instances', None):
                # zone_path is e.g. "zones/europe-west10-a"
                return zone_path.split('/')[-1]
        return None

    def delete_runner_instance(self, instance_name, delivery_id=None):
        """
        Delete a GCE instance.

        Args:
            instance_name (str): The name of the instance to delete.
            delivery_id (str): The GitHub webhook delivery ID for log correlation.
        """
        try:
            if len(self.zones) == 1:
                zone = self.zones[0]
            else:
                zone = self._find_instance_zone(instance_name)
                if zone is None:
                    logger.warning(
                        "Instance %s not found in any zone; skipping deletion "
                        "(already deleted?), delivery_id: %s",
                        instance_name,
                        delivery_id,
                    )
                    return
            logger.info(
                "Deleting GCE instance %s in zone %s, delivery_id: %s",
                instance_name,
                zone,
                delivery_id,
            )
            operation = self.instance_client.delete(
                project=self.project_id,
                zone=zone,
                instance=instance_name
            )
            logger.info(
                "Instance deletion operation started: %s, delivery_id: %s",
                operation.name,
                delivery_id,
            )
        except Exception as e:
            logger.error(
                "Failed to delete instance %s: %s, delivery_id: %s",
                instance_name,
                e,
                delivery_id,
            )
            raise
