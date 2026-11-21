locals {
  role_prefix = replace(local.prefix, "-", "_")
}

# Runtime identity of the protected Cloud Run service. It can call Gemini and read the
# data store / engine METADATA (to verify the ACL setting at request time). It has no
# permission to search, list or read documents, and no access to the bucket, so a
# compromised or confused agent cannot read protected content under its own identity.
resource "google_service_account" "agent" {
  account_id   = "${local.prefix}-agent"
  display_name = "${local.prefix} agent (Cloud Run, no document access)"
}

resource "google_project_iam_member" "agent_aiplatform" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.agent.email}"
}

resource "google_project_iam_custom_role" "verify" {
  role_id     = "${local.role_prefix}_verify"
  title       = "Protected search config verifier"
  description = "Read data store and engine metadata to verify the ACL setting. No document access."
  permissions = [
    "discoveryengine.dataStores.get",
    "discoveryengine.engines.get",
  ]
}

resource "google_project_iam_member" "agent_verify" {
  project = var.project_id
  role    = google_project_iam_custom_role.verify.name
  member  = "serviceAccount:${google_service_account.agent.email}"
}

# End users search under their OWN token. serviceusage.services.use lets the call be
# billed to this project (sent as x-goog-user-project); it grants no data access.
resource "google_project_iam_custom_role" "search_user" {
  role_id     = "${local.role_prefix}_search_user"
  title       = "Protected search user"
  description = "Search only; document ACLs decide what is returned."
  permissions = [
    "discoveryengine.servingConfigs.search",
    "serviceusage.services.use",
  ]
}

resource "google_project_iam_member" "search_users" {
  for_each = toset(var.search_members)
  project  = var.project_id
  role     = google_project_iam_custom_role.search_user.name
  member   = each.value
}

# Ingest operators: write objects to the private bucket and import documents.
resource "google_project_iam_custom_role" "ingest" {
  role_id     = "${local.role_prefix}_ingest"
  title       = "Protected corpus ingest operator"
  description = "Import documents into the protected data store and read operation status."
  permissions = [
    "discoveryengine.documents.import",
    "discoveryengine.dataStores.get",
    "discoveryengine.engines.get",
    "discoveryengine.operations.get",
    "discoveryengine.operations.list",
    "serviceusage.services.use",
  ]
}

resource "google_project_iam_member" "ingest_operators" {
  for_each = toset(var.ingest_operators)
  project  = var.project_id
  role     = google_project_iam_custom_role.ingest.name
  member   = each.value
}

resource "google_storage_bucket_iam_member" "ingest_bucket" {
  for_each = toset(var.ingest_operators)
  bucket   = google_storage_bucket.docs.name
  role     = "roles/storage.objectAdmin"
  member   = each.value
}

# Bucket metadata reads let the ingest script verify the bucket is private.
resource "google_storage_bucket_iam_member" "ingest_bucket_meta" {
  for_each = toset(var.ingest_operators)
  bucket   = google_storage_bucket.docs.name
  role     = "roles/storage.legacyBucketReader"
  member   = each.value
}

# Source deploys (gcloud run deploy --source) build as the project's default Compute service
# account. Organizations that no longer auto-grant it roles need this explicit, build-only role.
data "google_project" "this" {
  project_id = var.project_id
}

resource "google_project_iam_member" "build_sa" {
  count   = var.grant_build_roles ? 1 : 0
  project = var.project_id
  role    = "roles/cloudbuild.builds.builder"
  member  = "serviceAccount:${data.google_project.this.number}-compute@developer.gserviceaccount.com"

  depends_on = [google_project_service.apis]
}
