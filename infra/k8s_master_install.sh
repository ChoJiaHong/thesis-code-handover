#!/bin/bash

# Exit immediately if a command exits with a non-zero status
set -e

# Add Kubernetes APT repository
echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.28/deb/ /" | sudo tee /etc/apt/sources.list.d/kubernetes.list

# Add Kubernetes GPG key
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.28/deb/Release.key | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg

# Update package list and install Kubernetes components
sudo apt-get update
sudo apt-get install -y kubeadm kubelet kubectl
sudo apt-mark hold kubeadm kubelet kubectl

# Create the containerd configuration directory if it doesn't exist
sudo mkdir -p /etc/containerd

# Generate the default containerd configuration and save it to /etc/containerd/config.toml
sudo containerd config default | sudo tee /etc/containerd/config.toml > /dev/null

# Update SystemdCgroup setting in the containerd configuration file
sudo sed -i '/\[plugins\."io\.containerd\.grpc\.v1\.cri"\.containerd\.runtimes\.runc\.options\]/,/^\s*\[/ s/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml

# Restart containerd and kubelet services
sudo systemctl restart containerd
sudo systemctl restart kubelet

# Install Kubernetes CNI
sudo apt-get install -y kubernetes-cni

# Disable swap
sudo swapoff -a

# Initialize Kubernetes cluster
sudo kubeadm init --pod-network-cidr=10.244.0.0/16

# Set up kubeconfig for the current user
mkdir -p $HOME/.kube
sudo cp -i /etc/kubernetes/admin.conf $HOME/.kube/config
sudo chown $(id -u):$(id -g) $HOME/.kube/config

# Export kubeconfig path to bash profile
if [[ $SHELL == */bash ]]; then
  PROFILE_FILE="$HOME/.bash_profile"
elif [[ $SHELL == */zsh ]]; then
  PROFILE_FILE="$HOME/.zshrc"
else
  PROFILE_FILE="$HOME/.profile"
fi

if [ ! -f "$PROFILE_FILE" ]; then
  touch "$PROFILE_FILE"
fi

echo "export KUBECONFIG=/etc/kubernetes/admin.conf" >> "$PROFILE_FILE"
source "$PROFILE_FILE"

# Adjust permissions for admin.conf
sudo chmod 644 /etc/kubernetes/admin.conf

# Deploy Flannel CNI
kubectl apply -f https://raw.githubusercontent.com/coreos/flannel/master/Documentation/kube-flannel.yml

echo "Kubernetes cluster setup is complete!"
