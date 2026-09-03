#!/bin/bash

# Check if the script is run as root or with sudo privileges
if [ "$EUID" -ne 0 ]; then
  echo "This script must be run with sudo or as root."
  exit 1
fi

# Exit immediately if a command exits with a non-zero status
set -e

# Create the containerd configuration directory if it doesn't exist
sudo mkdir -p /etc/containerd

# Generate the default containerd configuration and save it to /etc/containerd/config.toml
sudo containerd config default | sudo tee /etc/containerd/config.toml > /dev/null

# Update SystemdCgroup setting in the containerd configuration file
sudo sed -i '/\[plugins\."io\.containerd\.grpc\.v1\.cri"\.containerd\.runtimes\.runc\.options\]/,/^\s*\[/ s/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml

# Restart containerd and kubelet services
sudo systemctl restart containerd
sudo systemctl restart kubelet

# Confirm changes
echo "Containerd and Kubelet have been restarted with updated configuration."