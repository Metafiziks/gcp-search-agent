# Vertex AI Search data store (unstructured documents from GCS)
resource "google_discovery_engine_data_store" "docs" {
  project                     = var.project_id
  location                    = "global"
  data_store_id               = "${var.env_name}-docs"
  display_name                = "${var.env_name} Document Store"
  industry_vertical           = "GENERIC"
  content_config              = "CONTENT_REQUIRED"
  solution_types              = ["SOLUTION_TYPE_SEARCH"]
  create_advanced_site_search = false

  depends_on = [google_project_service.apis]
}

# Vertex AI Search engine with LLM add-on (enables extractive answers + summaries)
resource "google_discovery_engine_search_engine" "search" {
  project        = var.project_id
  location       = "global"
  collection_id  = "default_collection"
  engine_id      = "${var.env_name}-search"
  display_name   = "${var.env_name} Search Engine"
  data_store_ids = [google_discovery_engine_data_store.docs.data_store_id]

  search_engine_config {
    search_tier    = "SEARCH_TIER_ENTERPRISE"
    search_add_ons = ["SEARCH_ADD_ON_LLM"]
  }

  depends_on = [google_discovery_engine_data_store.docs]
}
