
resource "azurerm_resource_group" "dockhealer" {
  name     = var.resource_group_name
  location = var.location

  tags = {
    project     = "DockHealer"
    environment = "development"
    managed_by  = "Terraform"
  }
}

resource "azurerm_virtual_network" "dockhealer" {
  name                = "dockhealer-vnet"
  address_space       = ["10.20.0.0/16"]
  location            = azurerm_resource_group.dockhealer.location
  resource_group_name = azurerm_resource_group.dockhealer.name

  tags = {
    project    = "DockHealer"
    managed_by = "Terraform"
  }
}

resource "azurerm_subnet" "dockhealer" {
  name                 = "dockhealer-subnet"
  resource_group_name  = azurerm_resource_group.dockhealer.name
  virtual_network_name = azurerm_virtual_network.dockhealer.name
  address_prefixes     = ["10.20.1.0/24"]
}

resource "azurerm_network_security_group" "dockhealer" {
  name                = "dockhealer-nsg"
  location            = azurerm_resource_group.dockhealer.location
  resource_group_name = azurerm_resource_group.dockhealer.name

  security_rule {
    name                       = "Allow-SSH-From-My-IP"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = var.ssh_allowed_cidr
    destination_address_prefix = "*"
  }

  tags = {
    project    = "DockHealer"
    managed_by = "Terraform"
  }
}

resource "azurerm_public_ip" "dockhealer" {
  name                = "dockhealer-public-ip"
  location            = azurerm_resource_group.dockhealer.location
  resource_group_name = azurerm_resource_group.dockhealer.name

  allocation_method = "Static"
  sku               = "Standard"

  tags = {
    project    = "DockHealer"
    managed_by = "Terraform"
  }
}

resource "azurerm_network_interface" "dockhealer" {
  name                = "dockhealer-nic"
  location            = azurerm_resource_group.dockhealer.location
  resource_group_name = azurerm_resource_group.dockhealer.name

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.dockhealer.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.dockhealer.id
  }

  tags = {
    project    = "DockHealer"
    managed_by = "Terraform"
  }
}

resource "azurerm_network_interface_security_group_association" "dockhealer" {
  network_interface_id      = azurerm_network_interface.dockhealer.id
  network_security_group_id = azurerm_network_security_group.dockhealer.id
}


resource "azurerm_linux_virtual_machine" "dockhealer" {
  name                = "dockhealer-vm"
  computer_name       = "dockhealer"
  resource_group_name = azurerm_resource_group.dockhealer.name
  location            = azurerm_resource_group.dockhealer.location
  size                = var.vm_size

  admin_username                  = var.admin_username
  disable_password_authentication = true

  custom_data = filebase64("${path.module}/cloud-init.yaml")
  network_interface_ids = [
    azurerm_network_interface.dockhealer.id
  ]

  admin_ssh_key {
    username   = var.admin_username
    public_key = file(var.ssh_public_key_path)
  }

  os_disk {
    name                 = "dockhealer-os-disk"
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
    disk_size_gb         = 64
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts-gen2"
    version   = "latest"
  }

  boot_diagnostics {}

  tags = {
    project     = "DockHealer"
    environment = "development"
    managed_by  = "Terraform"
  }
}
