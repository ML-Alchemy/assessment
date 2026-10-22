output "vpc_name" {
  value = google_compute_network.relay.name
}

output "database_private_ip" {
  value     = google_sql_database_instance.relay.private_ip_address
  sensitive = true
}

output "webhook_service_uri" {
  value = google_cloud_run_v2_service.webhook.uri
}

output "artifact_repository" {
  value = google_artifact_registry_repository.relay.name
}
