output "docs_bucket" {
  value       = google_storage_bucket.docs.name
  description = "Private bucket for the protected corpus"
}

output "datastore_id" {
  value       = google_discovery_engine_data_store.docs.data_store_id
  description = "ACL-enabled data store ID"
}

output "search_engine_id" {
  value       = google_discovery_engine_search_engine.search.engine_id
  description = "Protected search engine ID"
}

output "agent_service_account" {
  value       = google_service_account.agent.email
  description = "Runtime service account of the protected Cloud Run service"
}
