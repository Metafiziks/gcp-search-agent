terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "aiplatform.googleapis.com",
    "discoveryengine.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# GCS bucket — public read so citation links work
resource "google_storage_bucket" "docs" {
  name                        = "${var.project_id}-${var.env_name}-docs"
  location                    = var.region
  force_destroy               = true
  uniform_bucket_level_access = true

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "public_read" {
  bucket = google_storage_bucket.docs.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}

# Artifact Registry for agent container images
resource "google_artifact_registry_repository" "agents" {
  repository_id = "${var.env_name}-agents"
  location      = var.region
  format        = "DOCKER"
  description   = "Container images for ${var.env_name} agents"

  depends_on = [google_project_service.apis]
}
