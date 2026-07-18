output "docs_bucket" {
  value       = google_storage_bucket.docs.name
  description = "GCS bucket name for the document corpus and model artifacts"
}

output "datastore_id" {
  value       = google_discovery_engine_data_store.docs.data_store_id
  description = "Vertex AI Search data store ID"
}

output "search_engine_id" {
  value       = google_discovery_engine_search_engine.search.engine_id
  description = "Vertex AI Search engine ID"
}

output "artifact_registry" {
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.agents.repository_id}"
  description = "Artifact Registry base URL for agent images"
}

output "agent_service_account" {
  value       = google_service_account.agent.email
  description = "Service account email for the Cloud Run agent"
}

output "wif_provider" {
  value       = google_iam_workload_identity_pool_provider.github.name
  description = "Workload Identity provider resource name — set as WIF_PROVIDER repo variable"
}

output "deployer_service_account" {
  value       = google_service_account.deployer.email
  description = "Deployer service account email — set as WIF_SERVICE_ACCOUNT repo variable"
}

output "bq_dataset_id" {
  value       = google_bigquery_dataset.observability.dataset_id
  description = "BigQuery dataset for agent telemetry"
}

output "gcs_model_path" {
  value       = "gs://${google_storage_bucket.docs.name}/models/iforest.pkl"
  description = "GCS path for the IsolationForest model artifact"
}
