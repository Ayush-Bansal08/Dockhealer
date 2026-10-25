
output "vm_name" {
  description = "Name of the DockHealer virtual machine"
  value       = azurerm_linux_virtual_machine.dockhealer.name
}

output "vm_public_ip" {
  description = "Public IP address of the DockHealer virtual machine"
  value       = azurerm_public_ip.dockhealer.ip_address
}

output "resource_group_name" {
  description = "Resource group containing DockHealer infrastructure"
  value       = azurerm_resource_group.dockhealer.name
}
