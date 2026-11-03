#!/bin/sh
# usage: kind-upgrade.sh <from-chart> <to-chart> <soak-binary>
# Creates a kind cluster of its own and changes a Weir release under load: from
# one chart to the other and back at one replica, a replacement that starts late,
# up to three replicas, a rollout of the three, and back down. Exits non-zero if
# a change does not complete, a caller saw any error or an outcome did not hold.
# It measures no throughput.
#
# The soak binary comes from weir-tests, built for the cluster's platform. KIND
# names the kind binary; helm, kubectl and Docker come from PATH.
set -eu
[ "$#" -eq 3 ] && [ -x "$3" ] || { sed -n 2p "$0" >&2; exit 2; }
FROM=$1 TO=$2 SOAK=$3
ROOT=$(cd "$(dirname "$0")/.." && pwd)
KIND=${KIND:-kind}
NODE_IMAGE=kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5
CLUSTER=weir-upgrade-$$
NS=weir
RELEASE=mongo
ADDRESS=$RELEASE-weir.$NS.svc.cluster.local.
DEPLOYMENT=$RELEASE-weir
SELECTOR=app.kubernetes.io/instance=$RELEASE
K="kubectl -n $NS"
PAUSE=${PAUSE:-5}
TEMPLATE=0
CREATED=
WORK=$(mktemp -d)
# Every command below talks to this cluster alone, whatever the caller's context is.
export KUBECONFIG="$WORK/kubeconfig"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

fail() {
  log "FAILED: $*"
  exit 1
}

diagnose() {
  $K get pods -o wide
  $K get events --sort-by=.lastTimestamp | tail -n 30
  $K logs -l "$SELECTOR" --all-containers --prefix --tail=30
  $K exec client -- sh -c 'tail -n 5 /work/soak.log; cat /work/soak.json'
}

cleanup() {
  code=$?
  set +e
  trap - EXIT
  if [ -n "$CREATED" ]; then
    [ "$code" -eq 0 ] || diagnose
    "$KIND" delete cluster --name "$CLUSTER" --kubeconfig "$KUBECONFIG"
  fi
  rm -rf "$WORK"
  exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

pods() { $K get pods -l "$SELECTOR" -o jsonpath='{.items[*].metadata.name}'; }

# settled reports that every replica runs the current template and no other Pod
# remains, terminating ones included.
settled() {
  counts=$($K get deployment "$DEPLOYMENT" -o jsonpath='{.metadata.generation} {.status.observedGeneration} {.spec.replicas} {.status.updatedReplicas} {.status.availableReplicas}')
  set -- $counts
  [ "$#" -eq 5 ] && [ "$1" = "$2" ] && [ "$3" = "$4" ] && [ "$3" = "$5" ] && [ "$3" = "$WANT" ] || return 1
  [ "$($K get pods -l "$SELECTOR" --no-headers | wc -l)" -eq "$3" ]
}

check_soak() {
  if [ -n "$SOAKING" ] && $K exec client -- test -e /work/soak.exit; then
    fail "the soak client ended early"
  fi
}

# finish <what> <replaced-pods> waits for a change and checks those Pods have left.
finish() {
  what=$1 replaced=$2
  deadline=$(($(date +%s) + 300))
  until settled; do
    [ "$(date +%s)" -lt "$deadline" ] || fail "$what did not finish"
    check_soak
    sleep 2
  done
  now=" $(pods) "
  for pod in $replaced; do
    case $now in *" $pod "*) fail "$what left $pod running" ;; esac
  done
  check_soak
  log "$what done"
  sleep "$PAUSE"
}

# change <what> <chart> <replicas> [rollout] applies a change to the release. A
# rollout changes the Pod template, so it replaces every replica whether or not
# the charts differ.
change() {
  WANT=$3
  replaced=
  if [ -n "${4:-}" ]; then
    replaced=$(pods)
    TEMPLATE=$((TEMPLATE + 1))
  fi
  helm -n "$NS" upgrade --install "$RELEASE" "$2" -f "$ROOT/examples/mongo.yaml" -f "$ROOT/tests/kind/values.yaml" --set "replicaCount=$WANT" --set-string "podAnnotations.rollout=$TEMPLATE" >/dev/null
  finish "$1" "$replaced"
}

# patch <what> <type> <patch> replaces every replica with a changed template.
patch() {
  replaced=$(pods)
  $K patch deployment "$DEPLOYMENT" --type="$2" -p "$3" >/dev/null
  finish "$1" "$replaced"
}

log "creating cluster $CLUSTER"
"$KIND" create cluster --name "$CLUSTER" --kubeconfig "$KUBECONFIG" --image "$NODE_IMAGE" --wait 180s
CREATED=yes
kubectl create namespace "$NS"
$K apply -f "$ROOT/tests/kind/fixtures.yaml"
$K wait --for=condition=Ready --timeout=300s pod/mongo pod/client
FIXTURE_IMAGE=$($K get pod client -o jsonpath='{.spec.containers[0].image}')
$K exec mongo -- mongosh --quiet --eval 'rs.initiate({_id: "weir", members: [{_id: 0, host: "mongo:27017"}]})'
deadline=$(($(date +%s) + 60))
until [ "$($K exec mongo -- mongosh --quiet --eval 'db.hello().isWritablePrimary')" = true ]; do
  [ "$(date +%s)" -lt "$deadline" ] || fail "MongoDB elected no primary"
  sleep 2
done
kubectl cp "$SOAK" "$NS/client:/work/weir-soak"
$K exec client -- chmod +x /work/weir-soak

# A slow or unreliable registry must not use up a rollout's time.
for chart in "$FROM" "$TO"; do
  image=$(helm template "$RELEASE" "$chart" -f "$ROOT/examples/mongo.yaml" -f "$ROOT/tests/kind/values.yaml" | sed -n 's/^ *image: *"*\([^"]*\)"*$/\1/p' | head -n 1)
  log "pulling $image"
  attempts=5
  until docker exec "$CLUSTER-control-plane" crictl pull "$image" >/dev/null; do
    attempts=$((attempts - 1))
    [ "$attempts" -gt 0 ] || fail "cannot pull $image"
    sleep 10
  done
done

SOAKING=
change "install" "$FROM" 1

# Streams live longer than a one-replica rollout, so they meet each replica's exit.
$K exec client -- sh -c "nohup sh -c '/work/weir-soak -no-errors -weir-address $ADDRESS:7447 -weir-diagnostics $ADDRESS:7449 -mongo-uri mongodb://mongo:27017/?directConnection=true -mongo-workers 40 -mongo-rate 400 -keys-per-worker 16 -duration 1h -settle 10s -operation-timeout 5s -sample-interval 10s -stream-lifetime 90s -output /work/soak & echo \$! > /work/soak.pid; wait \$!; echo \$? > /work/soak.exit' > /work/soak.log 2>&1 &"
SOAKING=yes
sleep 15
check_soak

change "chart upgrade at one replica" "$TO" 1 rollout
change "chart rollback at one replica" "$FROM" 1 rollout
change "chart upgrade at one replica" "$TO" 1 rollout
# Late enough that the old replica exits first unless the rollout waits for the replacement.
patch "replacement that starts late" strategic '{"spec":{"template":{"spec":{"initContainers":[{"name":"late-start","image":"'"$FIXTURE_IMAGE"'","command":["sleep","30"]}]}}}}'
patch "late start removed" json '[{"op":"remove","path":"/spec/template/spec/initContainers"}]'
change "scale up to three replicas" "$TO" 3
change "rollout of three replicas" "$TO" 3 rollout
change "scale down to one replica" "$TO" 1

$K exec client -- sh -c 'kill $(cat /work/soak.pid)'
deadline=$(($(date +%s) + 420))
until $K exec client -- test -s /work/soak.exit; do
  [ "$(date +%s)" -lt "$deadline" ] || fail "the soak client did not finish"
  sleep 2
done
[ "$($K exec client -- cat /work/soak.exit)" = 0 ] || fail "a caller saw an error or an outcome did not hold"
$K exec client -- cat /work/soak.json
log "passed"
