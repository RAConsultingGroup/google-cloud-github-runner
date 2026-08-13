#!/usr/bin/env bash

# Helper script to build GCE VM images for GitHub Actions Runners from startup script

#shellcheck disable=SC2154

set -e

# 45 minutes: a clean bake takes a few minutes, so this only fires on a wedged one.
MAX_WAIT_SECONDS=2700

TEMP_VM_NAME="${image_name}-builder-$(date +%s)"
DISK_NAME="ssd-${image_name}-builder-$(date +%s)"

echo "Building VM image: ${image_name}"
echo "Project ID: ${project_id}"
echo "Startup script: ${startup_script_gcs}"
echo "Machine type: ${machine_type}"
echo "Zone: ${zone}"
echo "Temporary VM: $TEMP_VM_NAME"
echo ""

# Step 1: Create GCE VM with startup script from GCS
# gcloud splits --metadata on commas, so prewarm-images is space-separated.
echo "[1/4] Creating temporary VM instance..."
gcloud compute instances create "$TEMP_VM_NAME" \
	--project="${project_id}" \
	--zone="${zone}" \
	--machine-type="${machine_type}" \
	--network-interface="stack-type=IPV4_ONLY,subnet=${subnet},no-address" \
	--metadata='enable-oslogin=true,startup-script-url=${startup_script_gcs},prewarm-images=${prewarm_images}' \
	--maintenance-policy="MIGRATE" \
	--provisioning-model="STANDARD" \
	--service-account="${service_account}" \
	--scopes="https://www.googleapis.com/auth/cloud-platform" \
	--create-disk="auto-delete=yes,boot=yes,name=$DISK_NAME,image=${image_family},mode=rw,type=${disk_type},size=${disk_size},provisioned-iops=${disk_provisioned_iops},provisioned-throughput=${disk_provisioned_throughput}" \
	--no-shielded-secure-boot \
	--shielded-vtpm \
	--shielded-integrity-monitoring \
	--reservation-affinity=any \
	--quiet

echo "VM instance created: $TEMP_VM_NAME"
echo ""

# Step 2: Wait until VM is terminated (startup script completes and shuts down)
echo "[2/4] Waiting for VM to terminate (startup script execution)..."
echo "This may take several minutes depending on the startup script..."

# The startup script shuts the VM down on its last line, so TERMINATED means it finished.
# A failure exits without shutting down - hence the timeout: without it a failed bake left
# this loop printing RUNNING forever, terraform apply never returned, and killing it leaked
# the builder VM and its disk. Deliberately no image is created on this path: a half-installed
# boot disk must never become the image the whole fleet boots from.
WAITED=0
while true; do
	STATUS=$(gcloud compute instances describe "$TEMP_VM_NAME" \
		--project="${project_id}" \
		--zone="${zone}" \
		--format="get(status)" --quiet 2>/dev/null || echo "NOTFOUND")

	if [ "$STATUS" = "TERMINATED" ]; then
		echo "VM has terminated successfully"
		break
	fi

	if [ "$WAITED" -ge "$MAX_WAIT_SECONDS" ]; then
		echo "Startup script did not finish within $MAX_WAIT_SECONDS seconds (status: $STATUS)." >&2
		echo "Check the serial console: gcloud compute instances get-serial-port-output $TEMP_VM_NAME --zone=${zone} --project=${project_id}" >&2
		gcloud compute instances delete "$TEMP_VM_NAME" \
			--project="${project_id}" \
			--zone="${zone}" \
			--quiet || echo "Could not delete $TEMP_VM_NAME - remove it and disk $DISK_NAME by hand" >&2
		exit 1
	fi

	echo "Current status: $STATUS - waiting 10 seconds..."
	sleep 10
	WAITED=$((WAITED + 10))
done

echo ""

# Step 3: Create disk image from the VM's boot disk
echo "[3/4] Creating disk image from VM boot disk..."
gcloud compute images create "${image_name}-v$(date -u +%Y-%m-%d)-$(date +%s)" \
	--project="${project_id}" \
	--source-disk="$DISK_NAME" \
	--source-disk-zone="${zone}" \
	--family="${image_name}" \
	--description="GitHub Actions runner image built from ${startup_script_gcs} on $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
	--storage-location="${region}" \
	--quiet

echo "Disk image created: ${image_name}"
echo ""

# Step 4: Delete the temporary VM
echo "[4/4] Cleaning up temporary VM..."
gcloud compute instances delete "$TEMP_VM_NAME" \
	--project="${project_id}" \
	--zone="${zone}" \
	--quiet

echo "Temporary VM deleted"
echo ""
echo "✓ Image build complete successfully: ${image_name}"
