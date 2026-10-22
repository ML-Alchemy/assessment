variable "project_id" {
  description = "GCP project for this environment"
  type        = string
}

variable "region" {
  description = "US-only region for customer data"
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Environment name"
  type        = string
  default     = "staging"
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production"
  }
}

variable "container_image" {
  description = "Immutable image digest, not a mutable tag"
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
