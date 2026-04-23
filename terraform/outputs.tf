output "app_name" {
  value = juju_application.temporal_admin_k8s.name
}

output "provides" {
  value = {
    admin = "admin"
  }
}

output "requires" {
  value = {
    temporal_host_info = "temporal-host-info"
  }
}
