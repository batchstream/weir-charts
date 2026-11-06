#!/bin/sh
# usage: kind-rollout.sh <soak-binary>
# Creates a kind cluster of its own and changes a release of this checkout's chart
# under load: rollouts at one replica, a replacement that starts late, up to three
# replicas, rollouts of the three, and back down. Exits non-zero if a change does
# not complete, a caller saw any error or an outcome did not hold. It measures no
# throughput.
#
# The soak binary comes from weir-tests, built for the cluster's platform. KIND
# names the kind binary; helm, kubectl and Docker come from PATH.
set -eu
[ "$#" -eq 1 ] && [ -x "$1" ] || { sed -n 2p "$0" >&2; exit 2; }
SOAK=$1
ROOT=$(cd "$(dirname "$0")/.." && pwd)
KIND=${KIND:-kind}
NODE_IMAGE=kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5
CLUSTER=weir-rollout-$$
NS=weir
RELEASE=search
DEPLOYMENT=$RELEASE-weir
ADDRESS=$DEPLOYMENT.$NS.svc.cluster.local.
SELECTOR=app.kubernetes.io/instance=$RELEASE
K="kubectl -n $NS"
H="helm --kube-context kind-$CLUSTER -n $NS"
PAUSE=${PAUSE:-5}
TEMPLATE=0
SOAKING=
CREATED=
WORK=$(mktemp -d)
# Every command below talks to this cluster alone, whatever the caller's context
# and Helm settings are.
export KUBECONFIG="$WORK/kubeconfig"
unset HELM_KUBEAPISERVER HELM_KUBEASGROUPS HELM_KUBEASUSER HELM_KUBECAFILE HELM_KUBECONTEXT HELM_KUBEINSECURE_SKIP_TLS_VERIFY HELM_KUBETLS_SERVER_NAME HELM_KUBETOKEN HELM_NAMESPACE

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

fail() {
  log "FAILED: $*"
  exit 1
}

diagnose() {
  $K get pods -o wide
  $K get events --sort-by=.lastTimestamp | tail -n 30
  $K logs -l "$SELECTOR" --all-containers --prefix --tail=30
  $K logs search --tail=10
  $K exec client -- sh -c "tail -n 5 /work/soak.log; cat /work/soak.json"
}

cleanup() {
  code=$?
  set +e
  trap - EXIT
  trap '' INT TERM
  if [ -n "$CREATED" ]; then
    [ "$code" -eq 0 ] || diagnose
    "$KIND" delete cluster --name "$CLUSTER" --kubeconfig "$KUBECONFIG"
  fi
  rm -rf "$WORK"
  exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# wait_for <failure> <seconds> <command...>
wait_for() {
  failure=$1 deadline=$(($(date +%s) + $2))
  shift 2
  until "$@"; do
    [ "$(date +%s)" -lt "$deadline" ] || fail "$failure"
    sleep 2
  done
}

pods() { $K get pods -l "$SELECTOR" -o jsonpath='{.items[*].metadata.name}'; }

namespace_ready() { $K get serviceaccount default >/dev/null 2>&1; }

# settled reports that every replica runs the current template and no other Pod
# remains, terminating ones included.
settled() {
  counts=$($K get deployment "$DEPLOYMENT" -o jsonpath='{.metadata.generation} {.status.observedGeneration} {.spec.replicas} {.status.updatedReplicas} {.status.availableReplicas}')
  set -- $counts
  [ "$#" -eq 5 ] && [ "$1" = "$2" ] && [ "$3" = "$4" ] && [ "$3" = "$5" ] && [ "$3" = "$WANT" ] || return 1
  [ "$($K get pods -l "$SELECTOR" --no-headers | wc -l)" -eq "$3" ]
}

check_soak() {
  [ -n "$SOAKING" ] || return 0
  if $K exec client -- test -e /work/soak.exit 2>/dev/null; then
    fail "the soak client ended early"
  fi
}

settled_while_soaking() {
  check_soak
  settled
}

# finish <what> <replaced-pods> waits for a change and checks those Pods have left.
finish() {
  what=$1 replaced=$2
  wait_for "$what did not finish" 300 settled_while_soaking
  now=" $(pods) "
  for pod in $replaced; do
    case $now in *" $pod "*) fail "$what left $pod running" ;; esac
  done
  check_soak
  log "$what done"
  sleep "$PAUSE"
}

# change <what> <replicas> [rollout] applies a change to the release. A rollout
# changes the Pod template, so it replaces every replica.
change() {
  WANT=$2
  replaced=
  if [ -n "${3:-}" ]; then
    replaced=$(pods)
    TEMPLATE=$((TEMPLATE + 1))
  fi
  $H upgrade --install "$RELEASE" "$ROOT/charts/weir" -f "$ROOT/examples/search.yaml" -f "$ROOT/tests/kind/values.yaml" --set "replicaCount=$WANT" --set-string "podAnnotations.rollout=$TEMPLATE" >/dev/null
  finish "$1" "$replaced"
}

# patch <what> <type> <patch> replaces every replica with a changed template.
patch() {
  replaced=$(pods)
  $K patch deployment "$DEPLOYMENT" --type="$2" -p "$3" >/dev/null
  finish "$1" "$replaced"
}

# A slow or unreliable registry must not use up a rollout's time.
pull() {
  image=$(helm template "$RELEASE" "$ROOT/charts/weir" -f "$ROOT/examples/search.yaml" -f "$ROOT/tests/kind/values.yaml" | sed -n 's/^ *image: *"*\([^"]*\)"*$/\1/p' | head -n 1)
  [ -n "$image" ] || fail "the chart does not render with its values"
  log "pulling $image"
  attempts=5
  until docker exec "$CLUSTER-control-plane" crictl pull "$image" >/dev/null; do
    attempts=$((attempts - 1))
    [ "$attempts" -gt 0 ] || fail "cannot pull $image"
    sleep 10
  done
}

soak_sampled() { $K exec client -- test -s /work/soak.jsonl 2>/dev/null; }

soak_finished() { $K exec client -- test -s /work/soak.exit 2>/dev/null; }

log "creating cluster $CLUSTER"
CREATED=yes
"$KIND" create cluster --name "$CLUSTER" --kubeconfig "$KUBECONFIG" --image "$NODE_IMAGE" --wait 180s
kubectl create namespace "$NS"
wait_for "the namespace has no service account" 60 namespace_ready
$K apply -f "$ROOT/tests/kind/fixtures.yaml"
$K wait --for=condition=Ready --timeout=600s pod/search pod/client
FIXTURE_IMAGE=$($K get pod client -o jsonpath='{.spec.containers[0].image}')
pull

change "install" 1

# Streams live longer than a one-replica rollout, so they meet each replica's exit.
kubectl cp "$SOAK" "$NS/client:/work/soak.bin"
$K exec client -- chmod +x /work/soak.bin
$K exec client -- sh -c "nohup sh -c '/work/soak.bin -no-errors -weir-address $ADDRESS:7447 -weir-diagnostics $ADDRESS:7449 -search-url http://search:9200 -search-workers 40 -search-rate 400 -keys-per-worker 16 -duration 1h -settle 10s -operation-timeout 5s -sample-interval 10s -stream-lifetime 90s -output /work/soak & echo \$! > /work/soak.pid; wait \$!; echo \$? > /work/soak.exit' > /work/soak.log 2>&1 &"
SOAKING=yes
wait_for "the soak client reported no sample" 120 soak_sampled
check_soak

change "rollout at one replica" 1 rollout
change "rollout at one replica" 1 rollout
# Late enough that the old replica exits first unless the rollout waits for the replacement.
patch "replacement that starts late" strategic '{"spec":{"template":{"spec":{"initContainers":[{"name":"late-start","image":"'"$FIXTURE_IMAGE"'","command":["sleep","30"]}]}}}}'
patch "late start removed" json '[{"op":"remove","path":"/spec/template/spec/initContainers"}]'
change "scale up to three replicas" 3
change "rollout at three replicas" 3 rollout
change "rollout at three replicas" 3 rollout
change "scale down to one replica" 1

check_soak
SOAKING=
$K exec client -- sh -c 'kill $(cat /work/soak.pid)' || true
wait_for "the soak client did not finish" 420 soak_finished
log "soak client report"
$K exec client -- cat /work/soak.json || true
[ "$($K exec client -- cat /work/soak.exit)" = 0 ] || fail "a caller saw an error or an outcome did not hold"
log "passed"
