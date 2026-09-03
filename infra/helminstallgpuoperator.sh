helm repo add nvidia https://helm.ngc.nvidia.com/nvidia \
    && helm repo update
helm install --wait --generate-name \
    --version 24.3.0 \
    -n gpu-operator --create-namespace \
    nvidia/gpu-operator
kubectl apply -f time-slicing-config-all.yaml
kubectl patch clusterpolicies.nvidia.com/cluster-policy \
    -n gpu-operator --type merge \
    -p '{"spec": {"devicePlugin": {"config": {"name": "time-slicing-config-all", "default": "any"}}}}'