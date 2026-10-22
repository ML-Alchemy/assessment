provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  prefix = "relay-${var.environment}"
}

resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "compute.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudkms.googleapis.com",
    "artifactregistry.googleapis.com",
    "iam.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    "servicenetworking.googleapis.com",
  ])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_compute_network" "relay" {
  name                    = "${local.prefix}-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "relay" {
  name                     = "${local.prefix}-us-central1"
  region                   = var.region
  network                  = google_compute_network.relay.id
  ip_cidr_range            = "10.40.0.0/20"
  private_ip_google_access = true
}

resource "google_compute_global_address" "private_services" {
  name          = "${local.prefix}-private-services"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.relay.id
}

resource "google_service_networking_connection" "private_services" {
  network                 = google_compute_network.relay.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_services.name]
  depends_on              = [google_project_service.apis]
}

resource "google_sql_database_instance" "relay" {
  name                = "${local.prefix}-postgres"
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = var.environment == "production"

  settings {
    tier              = "db-f1-micro"
    availability_type = var.environment == "production" ? "REGIONAL" : "ZONAL"
    disk_autoresize   = true

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }

    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.relay.id
      ssl_mode        = "ENCRYPTED_ONLY"
    }

    database_flags {
      name  = "log_min_duration_statement"
      value = "1000"
    }
  }
  depends_on = [google_service_networking_connection.private_services]
}

resource "google_sql_database" "relay" {
  name     = "relay"
  instance = google_sql_database_instance.relay.name
}

resource "google_kms_key_ring" "relay" {
  name     = "${local.prefix}-keyring"
  location = var.region
}

resource "google_kms_crypto_key" "crm_tokens" {
  name            = "${local.prefix}-crm-token-key"
  key_ring        = google_kms_key_ring.relay.id
  rotation_period = "7776000s"
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_secret_manager_secret" "database_url" {
  secret_id = "${local.prefix}-database-url"
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_service_account" "api" {
  account_id   = "${local.prefix}-api"
  display_name = "Relay webhook API"
}

resource "google_service_account" "worker" {
  account_id   = "${local.prefix}-worker"
  display_name = "Relay worker"
}

resource "google_service_account" "gateway" {
  account_id   = "${local.prefix}-llm-gateway-a"
  display_name = "Relay LLM policy gateway A"
}

resource "google_service_account" "gateway_b" {
  account_id   = "${local.prefix}-llm-gateway-b"
  display_name = "Relay LLM policy gateway B"
}

resource "google_service_account" "admin" {
  account_id   = "${local.prefix}-admin"
  display_name = "Relay CRM admin"
}

resource "google_secret_manager_secret_iam_member" "worker_database" {
  secret_id = google_secret_manager_secret.database_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_secret_manager_secret_iam_member" "gateway_database" {
  secret_id = google_secret_manager_secret.database_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_secret_manager_secret_iam_member" "gateway_b_database" {
  secret_id = google_secret_manager_secret.database_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.gateway_b.email}"
}


resource "google_secret_manager_secret" "runtime_secrets" {
  for_each = toset([
    "crm-client-secret",
    "crm-token-key-material",
    "llm-gateway-key",
    "provider-a-api-key",
    "provider-b-api-key",
    "admin-state-key",
    "admin-password",
    "webhook-secret-cust-fernway",
    "webhook-secret-cust-halden",
    "webhook-secret-cust-quarry",
  ])
  secret_id = "${local.prefix}-${each.key}"
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "api_database" {
  secret_id = google_secret_manager_secret.database_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret_iam_member" "admin_database" {
  secret_id = google_secret_manager_secret.database_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.admin.email}"
}

resource "google_secret_manager_secret_iam_member" "worker_runtime" {
  for_each  = toset(["crm-client-secret", "crm-token-key-material", "llm-gateway-key"])
  secret_id = google_secret_manager_secret.runtime_secrets[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_secret_manager_secret_iam_member" "gateway_runtime" {
  for_each  = toset(["llm-gateway-key", "provider-a-api-key"])
  secret_id = google_secret_manager_secret.runtime_secrets[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_secret_manager_secret_iam_member" "gateway_b_runtime" {
  for_each  = toset(["llm-gateway-key", "provider-b-api-key"])
  secret_id = google_secret_manager_secret.runtime_secrets[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.gateway_b.email}"
}

resource "google_secret_manager_secret_iam_member" "admin_runtime" {
  for_each  = toset(["crm-client-secret", "crm-token-key-material", "admin-state-key", "admin-password"])
  secret_id = google_secret_manager_secret.runtime_secrets[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.admin.email}"
}

resource "google_secret_manager_secret_iam_member" "webhook_runtime" {
  for_each  = toset(["webhook-secret-cust-fernway", "webhook-secret-cust-halden", "webhook-secret-cust-quarry"])
  secret_id = google_secret_manager_secret.runtime_secrets[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

resource "google_kms_crypto_key_iam_member" "worker_token_encryption" {
  crypto_key_id = google_kms_crypto_key.crm_tokens.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_kms_crypto_key_iam_member" "admin_token_encryption" {
  crypto_key_id = google_kms_crypto_key.crm_tokens.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_service_account.admin.email}"
}

resource "google_artifact_registry_repository" "relay" {
  location      = var.region
  repository_id = "${local.prefix}-images"
  description   = "Relay container images"
  format        = "DOCKER"
}

resource "google_cloud_run_v2_service" "webhook" {
  name     = "${local.prefix}-webhook"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.api.email
    scaling {
      min_instance_count = 1
      max_instance_count = 10
    }
    containers {
      image = var.container_image
      ports {
        container_port = 8000
      }
      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }
    }
    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = google_compute_network.relay.name
        subnetwork = google_compute_subnetwork.relay.name
      }
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "webhook_errors" {
  display_name = "${local.prefix}: webhook 5xx errors"
  combiner     = "OR"

  conditions {
    display_name = "Webhook 5xx rate"
    condition_threshold {
      filter          = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${google_cloud_run_v2_service.webhook.name}\" AND metric.type=\"run.googleapis.com/request_count\" AND metric.labels.response_code_class=\"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      duration        = "300s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }
  alert_strategy {
    auto_close = "1800s"
  }
  depends_on = [google_project_service.apis]
}
