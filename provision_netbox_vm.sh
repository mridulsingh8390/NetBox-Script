#!/usr/bin/env bash
#
# provision_netbox_vm.sh
#
# Creates the Azure infrastructure needed to host the NetBox deployment:
# resource group, VNet + subnet, NSG (SSH, NetBox UI, HTTPS), and the VM
# itself (Ubuntu 24.04 LTS). Run this BEFORE azure_to_netbox_sync.py --bootstrap.
#
# Edit the variables below, then run: bash provision_netbox_vm.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Variables - edit these
# ---------------------------------------------------------------------------

RESOURCE_GROUP="rg-netbox"
LOCATION="eastus"

VNET_NAME="vnet-netbox"
VNET_PREFIX="10.50.0.0/16"

SUBNET_NAME="subnet-netbox"
SUBNET_PREFIX="10.50.1.0/24"

NSG_NAME="nsg-netbox"

VM_NAME="netbox-vm"
VM_SIZE="Standard_D2s_v5"          # 2 vCPU / 8GB RAM - reasonable for NetBox + Postgres + Redis
VM_IMAGE="Canonical:ubuntu-24_04-lts:server:latest"   # Ubuntu 24.04 LTS "Noble Numbat", Gen2, per Canonical's official Azure docs
ADMIN_USERNAME="azureuser"

# ---------------------------------------------------------------------------
# 1. Resource group
# ---------------------------------------------------------------------------

az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION"

# ---------------------------------------------------------------------------
# 2. VNet + subnet
# ---------------------------------------------------------------------------

az network vnet create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$VNET_NAME" \
  --address-prefix "$VNET_PREFIX" \
  --subnet-name "$SUBNET_NAME" \
  --subnet-prefix "$SUBNET_PREFIX" \
  --location "$LOCATION"

# ---------------------------------------------------------------------------
# 3. Network Security Group + rules
# ---------------------------------------------------------------------------

az network nsg create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$NSG_NAME" \
  --location "$LOCATION"

# SSH (22) - open to all sources.
# SECURITY NOTE: this allows SSH brute-force attempts from anywhere on the
# internet. If you'd rather restrict it to just your own IP (recommended,
# costs nothing to do), comment out the line below and uncomment the two
# lines under it instead.
az network nsg rule create \
  --resource-group "$RESOURCE_GROUP" \
  --nsg-name "$NSG_NAME" \
  --name Allow-SSH \
  --priority 1000 \
  --direction Inbound \
  --access Allow \
  --protocol Tcp \
  --destination-port-ranges 22 \
  --source-address-prefixes '*' \
  --destination-address-prefixes '*'
# MY_IP=$(curl -s ifconfig.me)
# az network nsg rule create --resource-group "$RESOURCE_GROUP" --nsg-name "$NSG_NAME" --name Allow-SSH --priority 1000 --direction Inbound --access Allow --protocol Tcp --destination-port-ranges 22 --source-address-prefixes "${MY_IP}/32" --destination-address-prefixes '*'

# NetBox direct UI (8000) - open to all sources.
# SECURITY NOTE: this is plain HTTP (no TLS) - credentials travel unencrypted
# if accessed this way. Recommended: remove or restrict this rule once the
# Apache HTTPS reverse proxy (port 443) is set up, so 8000 isn't exposed
# publicly long-term.
az network nsg rule create \
  --resource-group "$RESOURCE_GROUP" \
  --nsg-name "$NSG_NAME" \
  --name Allow-NetBox-8000 \
  --priority 1010 \
  --direction Inbound \
  --access Allow \
  --protocol Tcp \
  --destination-port-ranges 8000 \
  --source-address-prefixes '*' \
  --destination-address-prefixes '*'

# HTTPS (443) - for the Apache reverse proxy setup
az network nsg rule create \
  --resource-group "$RESOURCE_GROUP" \
  --nsg-name "$NSG_NAME" \
  --name Allow-HTTPS \
  --priority 1020 \
  --direction Inbound \
  --access Allow \
  --protocol Tcp \
  --destination-port-ranges 443 \
  --source-address-prefixes '*' \
  --destination-address-prefixes '*'

# HTTP (80) - only needed for the HTTP->HTTPS redirect / Let's Encrypt validation
az network nsg rule create \
  --resource-group "$RESOURCE_GROUP" \
  --nsg-name "$NSG_NAME" \
  --name Allow-HTTP \
  --priority 1030 \
  --direction Inbound \
  --access Allow \
  --protocol Tcp \
  --destination-port-ranges 80 \
  --source-address-prefixes '*' \
  --destination-address-prefixes '*'

# Associate the NSG with the subnet
az network vnet subnet update \
  --resource-group "$RESOURCE_GROUP" \
  --vnet-name "$VNET_NAME" \
  --name "$SUBNET_NAME" \
  --network-security-group "$NSG_NAME"

# ---------------------------------------------------------------------------
# 4. VM
# ---------------------------------------------------------------------------

az vm create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$VM_NAME" \
  --image "$VM_IMAGE" \
  --size "$VM_SIZE" \
  --vnet-name "$VNET_NAME" \
  --subnet "$SUBNET_NAME" \
  --nsg "$NSG_NAME" \
  --public-ip-sku Standard \
  --admin-username "$ADMIN_USERNAME" \
  --generate-ssh-keys \
  --location "$LOCATION"

# ---------------------------------------------------------------------------
# Done - print connection info
# ---------------------------------------------------------------------------

VM_PUBLIC_IP=$(az vm show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$VM_NAME" \
  --show-details \
  --query publicIps \
  --output tsv)

echo ""
echo "======================================================================"
echo "VM created."
echo "  Public IP:  ${VM_PUBLIC_IP}"
echo "  SSH:        ssh ${ADMIN_USERNAME}@${VM_PUBLIC_IP}"
echo ""
echo "Next steps on the VM:"
echo "  1. Copy azure_to_netbox_sync.py to the VM"
echo "  2. az login (interactive, one-time)"
echo "  3. sudo python3 azure_to_netbox_sync.py --bootstrap --create-azure-sp --azure-subscription-id <sub-id>"
echo "======================================================================"
