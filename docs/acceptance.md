# Acceptance evidence

Date: 2026-09-29. Work in progress; this document does not declare production qualification.

Chart schema, lint, template, and five behavior/negative test groups pass locally
with Helm 3.17.0 and Python 3.14/PyYAML 6.0.2. Default security contexts, config
revision rollout, digest selection, lifecycle probes, external Secret boundary,
NetworkPolicy destinations, PDB, and invalid values are checked.

A dedicated `weir-acceptance-20260929` namespace on the existing EKS cluster is used
for real install, backend, SDK, lifecycle, and rollback checks. Kubernetes is
v1.36.3-eks-cb19647 on native Linux arm64. Only new task-owned resources are mutated.
No existing Secret content is read. Temporary MongoDB 8.0.32 and Elasticsearch
8.19.22 use disposable data and do not represent production database durability.

The cluster's AWS VPC CNI node agent has `--enable-network-policy=false`. Network
isolation is an open deployment gate: a namespace and successfully created policies
alone do not isolate traffic. Shared CNI configuration is not changed by this work.
The final image digest, precise commands/results, and outstanding scope will be
recorded after runtime validation and the coordinated product release.
