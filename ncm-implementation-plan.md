# NCM MLOps implementation plan

## 1. Start: DS uploads the model to S3

The workflow starts when DS uploads these three files through the existing publishing script. Model training is outside this plan.

```text
s3://fcp-model-artifacts/serving/new_customers/2.5.0/
  model.cbm
  model_meta.json
  golden_sample.json
```

Keep the current FastAPI S3 download/retry logic for the initial version. Defer `initContainer`. Reuse a compatible runtime image for model-only releases.

## 2. Current status of all 14 stages

**Done** = confirmed by your description or supplied files. **Not implemented** = explicitly identified as missing. **To verify** = not established from the available information; inspect existing implementation and build what is missing. These statuses are not a live infrastructure audit.

**Status summary: 3 Done, 1 Not implemented, 10 To verify.** Execution below describes the intended workflow; an automatic stage is not necessarily implemented yet.

| # | Stage | Execution | Current status | Remaining work |
|---|---|---|---|---|
| 1 | DS uploads the three model files to S3 | Scripted | **Done** | Define the completed-upload handoff and source checksums |
| 2 | Publishing script triggers Jenkins after all uploads succeed | Automatic | **To verify** | Configure the authenticated callback, retries, and publication-ID deduplication |
| 3 | Jenkins validates artifact and runtime compatibility | Automatic | **To verify** | Check required files, hashes, metadata, and compatible runtime |
| 4 | Jenkins updates shared Helm values and commits to Git | Automatic | **To verify** | Add a versioned candidate release without changing role assignments |
| 5 | Argo CD deploys the candidate in Kubernetes | Automatic | **To verify** | Confirm chart/Application wiring and separate candidate Deployment |
| 6 | FastAPI downloads the model from S3 with retries | Automatic | **Done** | Confirm the actual bucket/prefix and startup behavior for this artifact |
| 7 | Check readiness and predictions; mark version Available | Automatic | **To verify** | Verify loaded model identity, sample predictions, and capacity |
| 8 | Board/delegate selects an exact version for Champion/Challenger | **Manual** | **Not implemented** | Build the model-version assignment UI |
| 9 | Backend applies and verifies the selected assignment | Automatic | **To verify** | Implement/confirm Git update, Argo CD tracking, Service selector change, and audit |
| 10 | Board/delegate sets routing percentages | **Manual** | **Done** | Use the existing Generic Router configuration UI |
| 11 | Router applies the chosen policy and records the result | Automatic | **To verify** | Reuse existing router behavior; verify effective policy and audit coverage |
| 12 | Monitor predictions, collect feedback, and report results | Automatic | **To verify** | Confirm monitoring, label joins, reports, and alerts |
| 13 | Execute and verify a human-requested rollback | Automatic after a decision | **To verify** | Retain the previous release and support rollback through the same UIs/backend |
| 14 | Remove unused releases after retention expires | Automatic | **To verify** | Exclude assigned, pending, draining, and rollback-retained releases |

The two routine manual decisions are **model-version selection** and **routing percentages**. Each later promotion, traffic adjustment, or rollback requires another authorized human decision; the system executes that choice automatically.

## 3. How Jenkins is triggered

1. The DS publishing script waits until all three uploads succeed and records the model version, S3 location, source-file hashes, and a stable publication ID.
2. It calls the proposed Jenkins job `ncm-prepare-model` automatically. No extra `READY` file is required for this direct callback.
3. Jenkins validates the artifact, updates candidate configuration in Git, and follows Argo CD deployment.
4. FastAPI downloads and loads the model using its current logic. Checks pass before the version becomes Available.
5. Jenkins stops at availability. It does not select Champion/Challenger or change percentages.

Example job request inputs:

```text
POST /job/ncm-prepare-model/buildWithParameters

MODEL_FAMILY = new_customers
MODEL_VERSION = 2.5.0
S3_BUCKET = fcp-model-artifacts
S3_PREFIX = serving/new_customers/2.5.0/
TARGET_ENV = <permitted-environment>
PUBLICATION_ID = <stable-id>
ARTIFACT_SHA256_JSON = <publisher-supplied-file-hashes>
```

Submit parameters as authenticated form data using the [Jenkins Remote Access API](https://www.jenkins.io/doc/book/using/remote-access-api/). The job name and parameter contract are proposed, not confirmed existing configuration. Retried requests must reuse the publication ID and resume/deduplicate the operation. Existing S3 files need a verified backfill request; adding the callback does not submit old releases automatically.

Keep published artifact paths immutable. Check the actual JSON schemas and sample acceptance criteria before implementing validation. Failed validation, download, or readiness keeps the candidate unavailable and leaves current assignments unchanged.

## 4. What each configuration does

| Item | Responsibility |
|---|---|
| `kubernetes/kube.yaml` | Appears to be Helm values: image, environment, resources, probes, scaling, and Service settings. Recover its valid source; the supplied excerpt has formatting/repeated-key issues. |
| Helm templates | Generate Kubernetes Deployments and Services from shared values; avoid copying a full Champion block for each model. |
| Argo CD Application | Points to the Git repository/chart/environment and reconciles the rendered resources. |
| Kubernetes | Runs Pods and routes stable role Services to the selected release's Ready Pods. |

Git stores desired releases and assignments. Argo CD renders Helm and applies the result; see [Argo CD's Helm integration](https://argo-cd.readthedocs.io/en/stable/user-guide/helm/). Preserve `new-customers-model` and `new-customers-model-challenger` Service names/DNS. A human's version choice changes the desired Service selector through Git; it does not change the router's percentages.

**Configuration to resolve:** the supplied Challenger values reference `fraud-model-service-files-dev`, while the shown artifact is in `fcp-model-artifacts`. Confirm FastAPI's bucket and `serving/new_customers/<version>/` mapping. Also review existing differences in feature APIs, workers, and scaling before sharing configuration. TCP readiness alone does not prove that the expected model is loaded.

## 5. Practical first release

1. DS uploads model `2.5.0`; the script triggers Jenkins automatically.
2. Automation validates and deploys it, then marks it Available.
3. With Challenger live traffic confirmed at 0%, the board/delegate selects `2.5.0` as Challenger in the new assignment UI. The backend applies and verifies it.
4. The board/delegate chooses Champion 90% / Challenger 10% in the existing routing UI. The router applies that policy.
5. Monitoring reports results. Further model/percentage changes remain human decisions; rollback execution and cleanup use automation.

## 6. Automation target

For the 14 rows above, the planning target is **12 scripted/automatic stages (85.7%) and two manual decision stages (14.3%)**. This assumes the publishing script runs without an additional routine human submission; count any required manual upload/start, extra approval, traffic ramp, or incident intervention in actual operation.

This percentage describes workflow execution, not how much is already implemented or how much engineering effort is automated. Current completion cannot be calculated until the **To verify** items are checked. Rollback decisions repeat the version/percentage controls; row 13 counts their technical execution.
