kubectl create namespace arha-system
kubectl apply -f arha-logs-pv.yaml
kubectl apply -f arha-logs-pvc.yaml
kubectl apply -f arha-pv.yaml
kubectl apply -f arha-pvc.yaml
kubectl apply -f Controller_v2/controller-rolebinding.yaml
kubectl apply -f Controller_v2/controller-deployment-sleep.yaml
kubectl apply -f Controller_v2/controller-service.yaml