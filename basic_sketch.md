Ghaza / DS pipeline
       ↓
S3
new-customers/3.3/
       ↓
READY
       ↓
trigger Jenkins
       ↓
artifact validation
       ↓
Docker build
custom predictor + model 3.3
       ↓
ECR
new-customers-model:3.3
       ↓
Helm values update
       ↓
ArgoCD
       ↓
Kubernetes
new-customers-model-v3-3
       ↓
readiness/smoke test
       ↓
version 3.3 available for
Champion/Challenger/Shadow assignment