variable "project_id" {
  description = "GCP project ID (use a dedicated test or protected project)"
  type        = string
}

variable "region" {
  description = "GCP region for Cloud Run and the private bucket"
  type        = string
  default     = "us-central1"
}

variable "env_name" {
  description = "Environment name; resources are prefixed <env_name>-protected"
  type        = string
  default     = "search-agent"
}

variable "search_members" {
  description = "IAM members (user:, group:) allowed to search under their own identity. Document ACLs still decide what each of them can read."
  type        = list(string)
  default     = []
}

variable "backend_invokers" {
  description = "IAM members (serviceAccount:, group:) of the TRUSTED backend allowed to invoke the protected Cloud Run service"
  type        = list(string)
  default     = []
}

variable "ingest_operators" {
  description = "IAM members that run scripts/ingest_protected.py: write to the private bucket and import documents"
  type        = list(string)
  default     = []
}

variable "manage_acl_config" {
  description = "Set the project-wide Agent Search identity provider to Google identity (GSUITE). Set false if one is already configured."
  type        = bool
  default     = true
}

variable "idp_type" {
  description = "GSUITE (Google identity; the project's organization must be linked to a Cloud Identity or Workspace customer) or THIRD_PARTY (Workforce Identity Federation)"
  type        = string
  default     = "GSUITE"

  validation {
    condition     = contains(["GSUITE", "THIRD_PARTY"], var.idp_type)
    error_message = "idp_type must be GSUITE or THIRD_PARTY."
  }
}

variable "workforce_pool_name" {
  description = "For THIRD_PARTY: locations/global/workforcePools/<pool-id>"
  type        = string
  default     = ""
}

variable "grant_build_roles" {
  description = "Grant the default Compute service account the Cloud Build builder role so source deploys work in organizations that no longer auto-grant it roles"
  type        = bool
  default     = true
}
