The cleanest solution is to treat **model versions as immutable deployments** and **Champion / Challenger / Shadow as movable aliases**.

Do not rebuild or rename a deployment when `2.1` becomes Champion. Do not manually edit DNS. Your three existing Kubernetes service names should stay permanent:

```text
new-customers-model-champion.fcp-dev.svc.cluster.local
new-customers-model-challenger.fcp-dev.svc.cluster.local
new-customers-model-shadow.fcp-dev.svc.cluster.local
```

What changes is only **which model-version pods each Service selects**.

### Recommended architecture

```text
                         Generic Router
                              |
              +---------------+---------------+
              |               |               |
              v               v               v
        Champion SVC     Challenger SVC    Shadow SVC
        stable DNS        stable DNS        stable DNS
              |               |               |
          selector          selector         selector
        version=2.1       version=3.0      version=3.2
              |               |               |
              v               v               v
         Deployment       Deployment       Deployment
           v2.1             v3.0             v3.2
        replicas=N        replicas=N        replicas=N
```

The important distinction is that Champion/Challenger/Shadow are **roles**, not deployments.

Your deployments should instead look like:

```text
new-customers-model-v1-0
new-customers-model-v1-1
new-customers-model-v2-1
new-customers-model-v3-0
new-customers-model-v3-2
```

Each deployment has labels identifying its immutable model version.

For example, `2.1`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: new-customers-model-v2-1
  namespace: fcp-dev

spec:
  replicas: 3

  selector:
    matchLabels:
      app: new-customers-model
      model-version: "2.1"

  template:
    metadata:
      labels:
        app: new-customers-model
        model-version: "2.1"

    spec:
      containers:
        - name: predictor
          image: <ECR>/new-customers-runtime:1.5.0

          env:
            - name: MODEL_NAME
              value: "mt_new_customers"

            - name: MODEL_VERSION
              value: "2.1"

            - name: MODEL_URI
              value: "s3://models/mt_new_customers/v2/"
```

Then your permanent Champion service is:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: new-customers-model-champion
  namespace: fcp-dev

spec:
  selector:
    app: new-customers-model
    model-version: "2.1"

  ports:
    - port: 80
      targetPort: 8080
```

Challenger:

```yaml
spec:
  selector:
    app: new-customers-model
    model-version: "3.0"
```

Shadow:

```yaml
spec:
  selector:
    app: new-customers-model
    model-version: "3.2"
```

Now Kubernetes automatically creates endpoints containing only pods belonging to that version.

Your router continues calling:

```text
Champion
new-customers-model-champion.fcp-dev.svc.cluster.local

Challenger
new-customers-model-challenger.fcp-dev.svc.cluster.local

Shadow
new-customers-model-shadow.fcp-dev.svc.cluster.local
```

It does not care whether Champion is `2.1`, `3.0`, or `3.2`.

---

## What happens when you promote a model

Suppose current state is:

```text
Champion    2.1
Challenger  3.0
Shadow      3.2
```

Then your UI changes it to:

```text
Champion    3.0
Challenger  3.2
Shadow      3.3
```

You do **not** redeploy 3.0.

You change the selectors:

```text
champion service:
    model-version: 3.0

challenger service:
    model-version: 3.2

shadow service:
    model-version: 3.3
```

Kubernetes updates the EndpointSlices.

The Champion DNS remains:

```text
new-customers-model-champion.fcp-dev.svc.cluster.local
```

but traffic now goes to the `3.0` pods.

That is exactly the abstraction you need.

---

# I would change one part of your current model pipeline

You said:

> MLOps pipeline builds an ECR image containing the custom prediction runtime + model artifact.

You can make this much cleaner.

I would **not rebuild the FastAPI runtime image for every model version** unless there is a strong technical reason.

Separate:

```text
Runtime version
       ≠
Model version
```

For example:

```text
ECR
predictor-runtime:1.4.0
predictor-runtime:1.5.0
predictor-runtime:1.6.0
```

and:

```text
S3

models/
└── mt_new_customers/
    ├── v2.1/
    │   ├── model_meta.json
    │   ├── model.cbm
    │   ├── speciality/
    │   ├── decision_band_info.json
    │   ├── feature_map.json
    │   └── checksums.sha256
    │
    ├── v3.0/
    └── v3.2/
```

Then deployment `v3.2` runs:

```text
predictor-runtime:1.5.0
        +
s3://.../mt_new_customers/v3.2/
```

At startup:

```text
Pod starts
   ↓
Download model artifact
   ↓
Verify checksum
   ↓
Load model
   ↓
FastAPI /ready = 200
   ↓
Service starts sending traffic
```

You can implement the download using either an init container or your runtime startup code.

I prefer an **initContainer + emptyDir**:

```text
Pod
│
├── initContainer
│      ↓
│   S3 download
│      ↓
│   checksum verification
│
├── shared emptyDir
│      /models/model.cbm
│
└── FastAPI predictor
       loads /models/model.cbm
```

Then changing from `3.1` → `3.2` does not require rebuilding the FastAPI application image.

That's a major simplification.

If you need to keep the current model-inside-image approach temporarily, the Champion/Challenger/Shadow Service design still works exactly the same.

---

# Then automate this through ArgoCD

Since you are already using Kubernetes + Helm + ArgoCD, I would make the assignment declarative.

For example:

```yaml
# values-dev.yaml

model: mt_new_customers

deployedVersions:
  "2.1":
    runtimeVersion: "1.5.0"
    artifactUri: "s3://models/mt_new_customers/v2.1/"
    replicas: 10

  "3.0":
    runtimeVersion: "1.5.0"
    artifactUri: "s3://models/mt_new_customers/v3.0/"
    replicas: 5

  "3.2":
    runtimeVersion: "1.5.0"
    artifactUri: "s3://models/mt_new_customers/v3.2/"
    replicas: 5

assignments:
  champion: "2.1"
  challenger: "3.0"
  shadow: "3.2"
```

Your Helm chart generates both:

```text
Deployments
new-customers-model-v2-1
new-customers-model-v3-0
new-customers-model-v3-2
```

and:

```text
Services
champion   → 2.1
challenger → 3.0
shadow     → 3.2
```

The Helm service template can be as simple as:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: new-customers-model-{{ .role }}

spec:
  selector:
    app: new-customers-model
    model-version: {{ .version | quote }}
```

---

# How the Board UI should work

This is where I would put the automation.

Do **not** let the browser directly call Kubernetes.

Put a small C/C Control API behind it.

```text
Board UI
   |
   | PUT assignment
   v
C/C Control API
   |
   +--- validate version
   |
   +--- update desired state
   |
   +--- trigger GitOps
   v
Config Git Repo
   |
   v
ArgoCD
   |
   v
Kubernetes
```

For example UI sends:

```http
PUT /models/mt_new_customers/assignments
```

```json
{
  "champion": "3.0",
  "challenger": "3.2",
  "shadow": "3.3"
}
```

The backend should first check:

```text
Does 3.0 exist?
Is 3.0 deployed?
Are replicas Ready?
Does /health return OK?
Does /metadata report model_version=3.0?
Does checksum match registry?
```

Only after those checks succeed should it change the assignment.

---

# Very important: make the UI show readiness

Don't let the operator choose any arbitrary S3 model.

Something like:

| Version | Artifact | Deployment | Health  | Assignment |
| ------- | -------- | ---------- | ------- | ---------- |
| 2.1     | Ready    | Ready      | Healthy | Champion   |
| 3.0     | Ready    | Ready      | Healthy | Challenger |
| 3.1     | Ready    | Ready      | Healthy | —          |
| 3.2     | Ready    | Ready      | Healthy | Shadow     |
| 3.3     | Ready    | Deploying  | —       | —          |

The UI should disable promotion of `3.3` until it reaches:

```text
Artifact validated
       ↓
Deployment Ready
       ↓
Smoke test passed
       ↓
Eligible for assignment
```

---

# Your complete automated flow

I would implement the end-to-end pipeline like this:

```text
DS Pipeline
    |
    | training complete
    v
ClearML / Model Registry
    |
    | approved model
    v
S3
models/mt_new_customers/v3.3/
    |
    | final READY marker / metadata
    v
MLOps CI
    |
    ├── validate model_meta.json
    ├── validate checksums
    ├── validate required files
    ├── validate feature schema
    │
    v
Update deployment Git config
    |
    v
ArgoCD
    |
    v
Kubernetes
new-customers-model-v3-3
    |
    ├── download artifact
    ├── checksum verify
    ├── load model
    ├── readiness
    └── smoke test
    |
    v
Version = DEPLOYED / READY
    |
    v
Board UI allows assignment
    |
    | user chooses:
    | Champion = 3.0
    | Challenger = 3.2
    | Shadow = 3.3
    v
C/C Control API
    |
    ├── validate deployments
    ├── update values.yaml
    ├── create Git commit
    └── trigger/sync ArgoCD
             |
             v
Kubernetes Service selectors update
             |
             v
champion   → 3.0
challenger → 3.2
shadow     → 3.3
             |
             v
Post-deployment smoke tests
             |
             v
Assignment = ACTIVE
```

There should be **zero kubectl commands by a person** in the normal flow.

---

# Rollback becomes extremely simple

This is one of the biggest advantages.

Suppose you promote:

```text
Champion
2.1 → 3.0
```

and immediately see technical errors.

You do not rebuild anything.

Previous deployment `2.1` is still running.

Rollback simply becomes:

```yaml
assignments:
  champion: "2.1"
```

ArgoCD syncs.

```text
Champion DNS
      |
      v
2.1 pods
```

Rollback can take seconds rather than requiring an image build/redeployment.

I would keep at least the previous Champion deployment warm for some period specifically for this reason.

---

# Add a version endpoint to every predictor

I strongly recommend every runtime expose:

```http
GET /metadata
```

Response:

```json
{
  "model_name": "mt_new_customers",
  "model_version": "3.2",
  "runtime_version": "1.5.0",
  "artifact_uri": "s3://models/mt_new_customers/v3.2/",
  "artifact_sha256": "abc123...",
  "loaded": true
}
```

Then after assignment, automation can verify:

```text
curl champion/metadata

model_version == expected champion version
```

For example:

```text
new-customers-model-champion.../metadata
```

must return:

```json
{
  "model_version": "2.1"
}
```

That becomes a very useful automated deployment validation.

---

# Don't select a specific Pod

One small terminology change is important.

You don't want:

```text
Champion → exact Pod
```

because the Deployment may have:

```text
Pod 1
Pod 2
Pod 3
...
Pod 40
```

You want:

```text
Champion
    ↓
Service
    ↓
all healthy pods from Deployment version 2.1
```

Therefore the mapping is:

```text
ROLE → MODEL VERSION → DEPLOYMENT/REPLICASET → PODS
```

not:

```text
ROLE → POD
```

That preserves autoscaling and fault tolerance.

---

# One thing I would not do

Don't put a `champion` label directly onto the model deployment and keep changing it like:

```yaml
role: champion
```

Technically it can work, but it mixes two different concepts:

```text
immutable identity:
model-version=3.2

mutable operational assignment:
role=champion
```

Keep the deployment immutable and put the mutable mapping on the Service selector/control-plane configuration.

That makes audit and rollback much easier.

---

# GitOps vs directly patching Kubernetes

For IDT's architecture, I'd initially choose **GitOps**.

Your board backend changes:

```yaml
champion: 2.1
challenger: 3.0
shadow: 3.2
```

and ArgoCD applies it.

That gives you automatically:

```text
who changed it
when
old configuration
new configuration
rollback commit
audit history
desired state
```

Avoid having UI code execute:

```bash
kubectl patch service ...
```

directly.

If later you need role changes to happen almost instantly and Git/ArgoCD latency becomes a problem, the control API can patch the three Service selectors directly, while storing the authoritative mapping in a control-plane store. But that's a later optimization.

For the first production implementation, Git + Helm + ArgoCD is much easier to operate.

---

## The architecture I would implement

```text
                         CLEARML
                            |
                            v
DS Training -----------> S3 Model Registry
                         version 3.2
                            |
                            v
                       MLOps Pipeline
                            |
                            v
                      Git / Helm values
                            |
                         ArgoCD
                            |
                            v

 Kubernetes
 ────────────────────────────────────────────

 Runtime image: predictor-runtime:1.5

        +--------------------+
        | model-v2.1         |
        | MODEL_URI=v2.1     |
        +--------------------+

        +--------------------+
        | model-v3.0         |
        | MODEL_URI=v3.0     |
        +--------------------+

        +--------------------+
        | model-v3.2         |
        | MODEL_URI=v3.2     |
        +--------------------+

           ^         ^         ^
           |         |         |
       Champion  Challenger   Shadow
        Service    Service    Service
           ^         ^         ^
           +---------+---------+
                     |
                Generic Router
```

For your current system, I would implement this before introducing any additional serving platform such as KServe. Kubernetes Deployments, Services, Helm, ArgoCD, S3, your FastAPI predictor, and the Generic Router are sufficient for this requirement.

The key design rule is:

> **Deployments represent immutable model versions. Services represent mutable Champion/Challenger/Shadow roles. The board changes the role-to-version mapping, not the deployments.**
