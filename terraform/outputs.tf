output "app_name" {
  value = juju_application.temporal_admin_k8s.name
}

output "provides" {
  value = {
    admin = "admin"
  }
}
