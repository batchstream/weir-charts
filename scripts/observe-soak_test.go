package main

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type samplingFixture struct {
	Baseline        record
	Unavailable     int
	Status          int
	Body            string
	BodyDelay       time.Duration
	BadIdentity     bool
	BadHealth       bool
	PodsUnavailable bool
	PodReads        atomic.Int32
	JobReads        atomic.Int32
	UsageReads      atomic.Int32
	MetricsReads    atomic.Int32
}

func (f *samplingFixture) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/metrics" && r.Header.Get("Authorization") != "Bearer fake-task-token" {
		http.Error(w, "missing test token", http.StatusUnauthorized)
		return
	}
	encoder := json.NewEncoder(w)
	switch r.URL.Path {
	case "/api/v1/namespaces/test/pods":
		f.PodReads.Add(1)
		if f.PodsUnavailable {
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
			return
		}
		pods := append([]pod(nil), f.Baseline.Pods...)
		if f.BadIdentity {
			pods[0].Metadata.UID = "replacement"
		}
		pods = append(pods, f.Baseline.LoadPods...)
		list := podList{Items: pods}
		_ = encoder.Encode(list)
	case "/apis/batch/v1/namespaces/test/jobs/load":
		f.JobReads.Add(1)
		_ = encoder.Encode(f.Baseline.Job)
	case "/apis/metrics.k8s.io/v1beta1/namespaces/test/pods":
		count := f.UsageReads.Add(1)
		if count <= int32(f.Unavailable) {
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
			return
		}
		if f.Status != 0 {
			http.Error(w, "failure", f.Status)
			return
		}
		if f.BodyDelay > 0 {
			w.WriteHeader(http.StatusOK)
			w.(http.Flusher).Flush()
			select {
			case <-r.Context().Done():
				return
			case <-time.After(f.BodyDelay):
			}
		}
		if f.Body != "" {
			_, _ = w.Write([]byte(f.Body))
			return
		}
		_, _ = w.Write(f.Baseline.Usage)
	case "/metrics":
		f.MetricsReads.Add(1)
		if f.BadHealth {
			_, _ = w.Write([]byte("\nweir_node_ready 0\n"))
			return
		}
		_, _ = w.Write([]byte("\nweir_node_ready 1\n"))
	default:
		http.NotFound(w, r)
	}
}

func samplingObserver(t *testing.T, fixture *samplingFixture) observer {
	t.Helper()
	fixture.Baseline = baselineRecord()
	fixture.Baseline.Pods[0].Metadata.Labels = map[string]string{"app.kubernetes.io/instance": "weir"}
	fixture.Baseline.Pods[1].Metadata.Labels = map[string]string{"app": "mongo"}
	fixture.Baseline.Pods[2].Metadata.Labels = map[string]string{"app": "elasticsearch"}
	fixture.Baseline.LoadPods[0].Metadata.Labels = map[string]string{"job-name": "load"}
	server := httptest.NewServer(fixture)
	t.Cleanup(server.Close)
	host, port, err := net.SplitHostPort(strings.TrimPrefix(server.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	fixture.Baseline.Pods[0].Status.IP = host
	token := filepath.Join(t.TempDir(), "task-token")
	if err := os.WriteFile(token, []byte("fake-task-token"), 0600); err != nil {
		t.Fatal(err)
	}
	cfg := settings{Namespace: "test", Job: "load", Release: "weir", Replicas: 1, MetricsPort: port, RequireMetrics: true}
	result := observer{Client: server.Client(), Settings: cfg, APIURL: server.URL, TokenFile: token}
	return result
}

func samplingOutput(t *testing.T) *os.File {
	t.Helper()
	output, err := os.Create(filepath.Join(t.TempDir(), "samples.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = output.Close() })
	return output
}

func TestObserverMetrics503RecoversInsideExistingGap(t *testing.T) {
	fixture := samplingFixture{Unavailable: 1}
	observer := samplingObserver(t, &fixture)
	output := samplingOutput(t)
	window := samplingWindow{Baseline: fixture.Baseline, Deadline: time.Now().Add(3 * time.Second), Output: output}
	current, _, err := observer.sampleWithin(context.Background(), window)
	if err != nil {
		t.Fatal(err)
	}
	if fixture.PodReads.Load() != 2 || fixture.JobReads.Load() != 2 || fixture.UsageReads.Load() != 2 || fixture.MetricsReads.Load() != 2 {
		t.Fatal("recovery did not resample the complete Kubernetes state")
	}
	if err := validate(current, fixture.Baseline, 1); err != nil {
		t.Fatal(err)
	}
	body, err := os.ReadFile(output.Name())
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(body)), "\n")
	if len(lines) != 3 {
		t.Fatalf("want retry, recovery and complete sample; got %s", body)
	}
	var retry, recovered map[string]any
	if err := json.Unmarshal([]byte(lines[0]), &retry); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal([]byte(lines[1]), &recovered); err != nil {
		t.Fatal(err)
	}
	if retry["kind"] != "metrics_api_retry" || retry["status"] != float64(503) || retry["attempt"] != float64(1) || retry["attempt_elapsed_ns"].(float64) <= 0 || retry["remaining_ns"].(float64) <= 0 {
		t.Fatalf("missing retry audit: %v", retry)
	}
	if recovered["kind"] != "metrics_api_recovered" || recovered["attempts"] != float64(1) || recovered["elapsed_ns"].(float64) < float64(time.Second) {
		t.Fatalf("missing recovery duration: %v", recovered)
	}
}

func TestObserverMetrics503CannotRenewRemainingGap(t *testing.T) {
	fixture := samplingFixture{Unavailable: 1000}
	observer := samplingObserver(t, &fixture)
	output := samplingOutput(t)
	// The previous successful sample already consumed almost the whole 90s gap.
	previous := time.Now().Add(-90*time.Second + 150*time.Millisecond)
	window := samplingWindow{Baseline: fixture.Baseline, Deadline: previous.Add(90 * time.Second), Output: output}
	started := time.Now()
	_, _, err := observer.sampleWithin(context.Background(), window)
	if !errors.Is(err, context.DeadlineExceeded) || time.Since(started) > time.Second {
		t.Fatalf("renewed the remaining sampling budget: %v after %s", err, time.Since(started))
	}
	if fixture.UsageReads.Load() != 1 {
		t.Fatalf("unexpected retry beyond remaining budget: %d", fixture.UsageReads.Load())
	}
	body, err := os.ReadFile(output.Name())
	if err != nil || !strings.Contains(string(body), `"kind":"metrics_api_retry"`) || strings.Contains(string(body), `"pods"`) {
		t.Fatalf("lost failure audit or published an incomplete sample: %s, %v", body, err)
	}
}

func TestObserverSuccessfulHeadersCannotOutliveSamplingGap(t *testing.T) {
	fixture := samplingFixture{Unavailable: 1, BodyDelay: time.Second}
	observer := samplingObserver(t, &fixture)
	output := samplingOutput(t)
	window := samplingWindow{Baseline: fixture.Baseline, Deadline: time.Now().Add(1150 * time.Millisecond), Output: output}
	started := time.Now()
	_, _, err := observer.sampleWithin(context.Background(), window)
	if !errors.Is(err, context.DeadlineExceeded) || time.Since(started) > 2*time.Second {
		t.Fatalf("accepted response headers without bounded body processing: %v", err)
	}
	body, err := os.ReadFile(output.Name())
	if err != nil || !strings.Contains(string(body), `"kind":"metrics_api_retry"`) || strings.Contains(string(body), `"pods"`) || fixture.UsageReads.Load() != 2 {
		t.Fatalf("retried or published a late response: %s, %v", body, err)
	}
}

func TestObserverDoesNotRetryOtherFailures(t *testing.T) {
	for _, mode := range []string{"401", "403", "500", "malformed", "missing", "stale", "identity", "identity-with-503", "health", "pods-503"} {
		t.Run(mode, func(t *testing.T) {
			fixture := samplingFixture{}
			observer := samplingObserver(t, &fixture)
			switch mode {
			case "401":
				fixture.Status = 401
			case "403":
				fixture.Status = 403
			case "500":
				fixture.Status = 500
			case "malformed":
				fixture.Body = `{"items":`
			case "missing":
				fixture.Body = `{"items":[]}`
			case "stale":
				var usage usageList
				if err := json.Unmarshal(fixture.Baseline.Usage, &usage); err != nil {
					t.Fatal(err)
				}
				usage.Items[0].Timestamp = time.Now().Add(-3 * time.Minute)
				body, err := json.Marshal(usage)
				if err != nil {
					t.Fatal(err)
				}
				fixture.Body = string(body)
			case "identity":
				fixture.BadIdentity = true
			case "identity-with-503":
				fixture.BadIdentity = true
				fixture.Unavailable = 100
			case "health":
				fixture.BadHealth = true
				observer.Settings.RequireMetrics = true
			case "pods-503":
				fixture.PodsUnavailable = true
			}
			output := samplingOutput(t)
			window := samplingWindow{Baseline: fixture.Baseline, Deadline: time.Now().Add(3 * time.Second), Output: output}
			_, _, err := observer.sampleWithin(context.Background(), window)
			if err == nil || fixture.PodReads.Load() != 1 || fixture.UsageReads.Load() > 1 {
				t.Fatalf("accepted or retried non-transient failure: %v", err)
			}
			body, readErr := os.ReadFile(output.Name())
			if readErr != nil || len(body) != 0 {
				t.Fatalf("published failure as retry or sample: %s, %v", body, readErr)
			}
		})
	}
}

func baselineRecord() record {
	ready := containerStatus{Name: "weir", ImageID: "sha256:fixed", Ready: true, Restarts: 0}
	metadataValue := metadata{Name: "weir", UID: "pod-id"}
	state := podStatus{Phase: "Running", Containers: []containerStatus{ready}}
	first := pod{Metadata: metadataValue, Status: state}
	second := first
	second.Metadata = metadata{Name: "mongo", UID: "mongo-id"}
	third := first
	third.Metadata = metadata{Name: "search", UID: "search-id"}
	loadMeta := metadata{Name: "load", UID: "load-id"}
	load := pod{Metadata: loadMeta, Status: state}
	jobMeta := metadata{UID: "job-id"}
	runningStatus := jobStatus{Active: 1}
	runningJob := job{Metadata: jobMeta, Status: runningStatus}
	result := record{UTC: time.Now().UTC(), Pods: []pod{first, second, third}, LoadPods: []pod{load}, Job: runningJob}
	usage := usageList{}
	for _, item := range append([]pod{load}, result.Pods...) {
		measurement := containerUsage{Name: "weir", Usage: map[string]string{"cpu": "1m", "memory": "1Mi"}}
		metric := podUsage{Metadata: item.Metadata, Timestamp: result.UTC, Containers: []containerUsage{measurement}}
		usage.Items = append(usage.Items, metric)
	}
	result.Usage, _ = json.Marshal(usage)
	return result
}

func TestObserverRejectsLostLifecycleEvidence(t *testing.T) {
	baseline := baselineRecord()
	if err := validate(baseline, baseline, 1); err != nil {
		t.Fatal(err)
	}
	for _, mode := range []string{"replaced-service", "replaced-load", "replaced-job", "restart", "unready", "failed-job", "missing", "image"} {
		t.Run(mode, func(t *testing.T) {
			current := baselineRecord()
			switch mode {
			case "replaced-service":
				current.Pods[0].Metadata.UID = "new"
			case "replaced-load":
				current.LoadPods[0].Metadata.UID = "new"
			case "replaced-job":
				current.Job.Metadata.UID = "new"
			case "restart":
				current.Pods[0].Status.Containers[0].Restarts = 1
			case "unready":
				current.Pods[0].Status.Containers[0].Ready = false
			case "failed-job":
				current.Job.Status.Failed = 1
			case "image":
				current.Pods[0].Status.Containers[0].ImageID = "changed"
			case "missing":
				current.Pods = current.Pods[:2]
			}
			if err := validate(current, baseline, 1); err == nil {
				t.Fatal("accepted missing lifecycle evidence")
			}
		})
	}
}

func TestObserverCannotStartAfterTheLoad(t *testing.T) {
	baseline := baselineRecord()
	if err := baselineRunning(baseline); err != nil {
		t.Fatal(err)
	}
	baseline.Job.Status.Active = 0
	baseline.Job.Status.Succeeded = 1
	baseline.LoadPods[0].Status.Phase = "Succeeded"
	if err := baselineRunning(baseline); err == nil {
		t.Fatal("accepted late observer")
	}
}

func TestObserverRequiresDurableRunnerEvidence(t *testing.T) {
	dir := t.TempDir()
	cfg := settings{Report: filepath.Join(dir, "load.jsonl"), ExitStatus: filepath.Join(dir, "status.json"), RunID: "run-id", LoadDuration: time.Minute}
	if err := os.WriteFile(cfg.ExitStatus, []byte(`{"exit_code":0}`), 0600); err != nil {
		t.Fatal(err)
	}
	valid := `{"kind":"passed","run_id":"run-id","elapsed_ns":60000000000,"failures":0,"unknown":0}`
	if err := os.WriteFile(cfg.Report, []byte(valid), 0600); err != nil {
		t.Fatal(err)
	}
	if err := verifyLoad(cfg); err != nil {
		t.Fatal(err)
	}
	for _, invalid := range []string{
		`{"kind":"passed"}`,
		valid + "\n" + `{"kind":"passed"}`,
		`{"kind":"passed","run_id":"other","elapsed_ns":60000000000,"failures":0,"unknown":0}`,
		`{"kind":"passed","run_id":"run-id","elapsed_ns":100,"failures":0,"unknown":0}`,
		`{"kind":"passed","run_id":"run-id","elapsed_ns":60000000000,"failures":0,"unknown":1}`,
	} {
		if err := os.WriteFile(cfg.Report, []byte(invalid), 0600); err != nil {
			t.Fatal(err)
		}
		if err := verifyLoad(cfg); err == nil {
			t.Fatal("accepted invalid final report")
		}
	}
	if err := os.WriteFile(cfg.Report, []byte(valid), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(cfg.ExitStatus, []byte(`{}`), 0600); err != nil {
		t.Fatal(err)
	}
	if err := verifyLoad(cfg); err == nil {
		t.Fatal("accepted missing exit code")
	}
}

func TestObserverCannotReplaceExistingStatus(t *testing.T) {
	file := filepath.Join(t.TempDir(), "status.json")
	original := []byte(`{"passed":false}`)
	if err := exclusiveResult(file, original); err != nil {
		t.Fatal(err)
	}
	replacement := []byte(`{"passed":true}`)
	if err := exclusiveResult(file, replacement); err == nil {
		t.Fatal("overwrote existing evidence")
	}
	actual, err := os.ReadFile(file)
	if err != nil || string(actual) != string(original) {
		t.Fatal("changed existing evidence")
	}
}

func TestObserverRequiresFreshCompleteUsage(t *testing.T) {
	for _, mode := range []string{"missing-pod", "missing-memory", "stale"} {
		t.Run(mode, func(t *testing.T) {
			current := baselineRecord()
			var usage usageList
			if err := json.Unmarshal(current.Usage, &usage); err != nil {
				t.Fatal(err)
			}
			switch mode {
			case "missing-pod":
				usage.Items = usage.Items[:1]
			case "missing-memory":
				delete(usage.Items[0].Containers[0].Usage, "memory")
			case "stale":
				usage.Items[0].Timestamp = current.UTC.Add(-3 * time.Minute)
			}
			current.Usage, _ = json.Marshal(usage)
			if err := validateUsage(current); err == nil {
				t.Fatal("accepted incomplete usage evidence")
			}
		})
	}
}
