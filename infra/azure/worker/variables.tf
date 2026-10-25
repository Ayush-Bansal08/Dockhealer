
variable "location" {
  description = "Azure region for DockHealer infrastructure"
  type        = string
  default     = "centralindia"
}

variable "resource_group_name" {
  description = "Name of the DockHealer resource group"
  type        = string
  default     = "dockhealer-rg"
}

variable "vm_size" {
  description = "Azure VM size for the DockHealer worker"
  type        = string
  default     = "Standard_B2s_v2"
}

variable "admin_username" {
  description = "Linux administrator username"
  type        = string
  default     = "dockadmin"
}

variable "ssh_public_key_path" {
  description = "Path to the SSH public key on the machine running Terraform"
  type        = string
}

variable "ssh_allowed_cidr" {
  description = "Public IPv4 CIDR allowed to SSH into the VM"
  type        = string

  validation {
    condition     = can(cidrnetmask(var.ssh_allowed_cidr)) && can(regex("/32$", var.ssh_allowed_cidr))
    error_message = "Provide a single IPv4 address in CIDR notation, such as 203.0.113.10/32."
  }
}
