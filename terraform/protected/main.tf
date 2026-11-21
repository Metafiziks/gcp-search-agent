# Protected mode (KB_ACCESS_MODE=protected): a separate Terraform root with its own state.
# It never creates the public sample bucket, the sample data store, API keys, or the
# observability stack, and it leaves the default (sample) deployment untouched.
terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0"
    }
  }
}

provider "google" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
  billing_project       = var.project_id
}

locals {
  prefix = "${var.env_name}-protected"
}

resource "google_project_service" "apis" {
  for_each = toset([
    "cloudresourcemanager.googleapis.com",
    "run.googleapis.com",
    "aiplatform.googleapis.com",
    "discoveryengine.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "serviceusage.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# Private corpus bucket: no public access, no ACLs, no anonymous reads.
# Teardown wipes it (force_destroy), like the sample bucket.
resource "google_storage_bucket" "docs" {
  name                        = "${var.project_id}-${local.prefix}-docs"
  location                    = var.region
  force_destroy               = true
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  depends_on = [google_project_service.apis]
}

# Identity provider for access-controlled data stores. This is a per-location,
# project-wide Agent Search setting (one identity provider per location).
resource "google_discovery_engine_acl_config" "idp" {
  count    = var.manage_acl_config ? 1 : 0
  project  = var.project_id
  location = "global"

  idp_config {
    idp_type = var.idp_type

    dynamic "external_idp_config" {
      for_each = var.idp_type == "THIRD_PARTY" ? [1] : []
      content {
        workforce_pool_name = var.workforce_pool_name
      }
    }
  }

  depends_on = [google_project_service.apis]
}

# Dedicated ACL-enabled data store. acl_enabled is immutable, so protected mode can
# never be switched on for the sample store.
resource "google_discovery_engine_data_store" "docs" {
  project                     = var.project_id
  location                    = "global"
  data_store_id               = "${local.prefix}-docs"
  display_name                = "${local.prefix} Document Store"
  industry_vertical           = "GENERIC"
  content_config              = "CONTENT_REQUIRED"
  solution_types              = ["SOLUTION_TYPE_SEARCH"]
  create_advanced_site_search = false
  acl_enabled                 = true

  depends_on = [google_project_service.apis, google_discovery_engine_acl_config.idp]

  lifecycle {
    ignore_changes = [document_processing_config]
  }
}

# Search engine over ONLY the protected data store.
resource "google_discovery_engine_search_engine" "search" {
  project        = var.project_id
  location       = "global"
  collection_id  = "default_collection"
  engine_id      = "${local.prefix}-search"
  display_name   = "${local.prefix} Search Engine"
  data_store_ids = [google_discovery_engine_data_store.docs.data_store_id]

  search_engine_config {
    search_tier    = "SEARCH_TIER_ENTERPRISE"
    search_add_ons = ["SEARCH_ADD_ON_LLM"]
  }
}
