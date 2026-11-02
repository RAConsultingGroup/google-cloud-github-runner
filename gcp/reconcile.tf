# Cloud Scheduler calls the manager's /reconcile endpoint every minute with an OIDC token for this
# service account; the app accepts no other caller.
locals {
  reconcile_audience = "github-runners-manager-reconcile"
}

resource "google_service_account" "reconcile" {
  count        = var.github_runners_reconcile ? 1 : 0
  project      = module.project.project_id
  account_id   = "github-runners-reconcile"
  display_name = "Cloud Scheduler - GitHub Actions Runners reconcile (Terraform managed)"
}

resource "google_cloud_scheduler_job" "reconcile" {
  count   = var.github_runners_reconcile ? 1 : 0
  project = module.project.project_id
  # Cloud Scheduler is not offered in every Compute region (e.g. europe-west10); it can call the
  # manager in any region.
  region   = coalesce(var.github_runners_reconcile_region, var.region)
  name     = "github-runners-reconcile"
  schedule = "* * * * *"
  # A run caps its own work well inside this; the next run retries anything this one missed.
  attempt_deadline = "180s"
  retry_config {
    retry_count = 0
  }
  http_target {
    http_method = "POST"
    uri         = "${module.cloud_run_github_runners_manager.service_uri}/reconcile"
    oidc_token {
      service_account_email = google_service_account.reconcile[0].email
      audience              = local.reconcile_audience
    }
  }
}
