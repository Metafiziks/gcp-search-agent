variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region for Cloud Run and Artifact Registry"
  type        = string
  default     = "us-central1"
}

variable "env_name" {
  description = "Environment name — drives all resource names"
  type        = string
  default     = "search-agent"
}

variable "github_repo" {
  description = "GitHub repository in owner/repo format (for Workload Identity Federation)"
  type        = string
}
