# EKS network enforcement acceptance

The 2026-09-29 acceptance cluster initially had an AWS VPC CNI v1.21.1-eksbuild.7
node agent installed with network enforcement disabled. A controller-owned test
Job with deny-all egress could still fetch the task Elasticsearch Service. This
is a failed isolation check, not a successful deployment boundary.

A reviewed remediation uses the installed managed addon's enableNetworkPolicy
flag, preserving its version and standard startup mode. Before changing this
shared component, inventory every namespace and cluster policy, match selectors
against live workloads, and verify Linux kernel/node support. The inspected cluster
had four legacy policies in its telemetry namespace selecting no current Pods and
no cluster policies. A temporary telemetry allow-all policy can preserve its
currently effective connectivity while enforcement is activated for the test
namespace; this does not qualify telemetry's existing policy design.

No shared setting changes without the cluster owner's approval. The rollout must
observe addon and aws-node health, preserve telemetry readiness, and then verify
both allowed SDK/backend/DNS traffic and denied unlabeled/direct-IP traffic. Check
that additive allow policies do not undermine specific restrictions. Standard mode
has a startup interval before the CNI applies a new Pod's policy; it is not strict
zero-window isolation. Strict mode affects every workload's startup and needs a
separate cluster-wide connectivity review.

If unexpected application restrictions occur, an explicit allow-all overlay in
the affected approved namespace restores the prior connectivity while keeping
the network agent active. That rollback invalidates network acceptance. A full
addon disable is not equivalent to flipping a flag: AWS's documented procedure
requires removing network policies first, which needs separate review of each
owner's objects and preservation/restoration of their definitions.

Official procedures: [enable and prerequisites](https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy-configure.html),
[considerations](https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy.html),
and [disable](https://docs.aws.amazon.com/eks/latest/userguide/network-policy-disable.html).
