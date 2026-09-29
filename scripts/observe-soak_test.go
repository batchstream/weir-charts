package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
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
	UsageBodies     []string
	LargeMetrics    bool
	BadReadiness    bool
	BadRestart      bool
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
		pods[0].Status.Containers = append([]containerStatus(nil), pods[0].Status.Containers...)
		if f.BadReadiness {
			pods[0].Status.Containers[0].Ready = false
		}
		if f.BadRestart {
			pods[0].Status.Containers[0].Restarts = 1
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
		if int(count) <= len(f.UsageBodies) {
			_, _ = w.Write([]byte(f.UsageBodies[count-1]))
			return
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
		if f.LargeMetrics {
			_, _ = w.Write([]byte(strings.Repeat("# padding\n", 20000)))
		}
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
	cfg := settings{Namespace: "test", Job: "load", Release: "weir", Replicas: 1, MetricsPort: port, RequireMetrics: true, Output: filepath.Join(t.TempDir(), "observer.jsonl")}
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
	window := samplingWindow{Baseline: &fixture.Baseline, Deadline: time.Now().Add(3 * time.Second), Output: output}
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
	if retry["kind"] != "sample_retry" || retry["http_status"] != float64(503) || retry["attempt"] != float64(1) || retry["attempt_elapsed_ns"].(float64) <= 0 || retry["remaining_ns"].(float64) <= 0 {
		t.Fatalf("missing retry audit: %v", retry)
	}
	if recovered["kind"] != "sample_recovered" || recovered["attempts"] != float64(1) || recovered["elapsed_ns"].(float64) < float64(time.Second) {
		t.Fatalf("missing recovery duration: %v", recovered)
	}
}

func TestObserverMetrics503CannotRenewRemainingGap(t *testing.T) {
	fixture := samplingFixture{Unavailable: 1000}
	observer := samplingObserver(t, &fixture)
	output := samplingOutput(t)
	// The previous successful sample already consumed almost the whole 90s gap.
	previous := time.Now().Add(-90*time.Second + 150*time.Millisecond)
	window := samplingWindow{Baseline: &fixture.Baseline, Deadline: previous.Add(90 * time.Second), Output: output}
	started := time.Now()
	_, _, err := observer.sampleWithin(context.Background(), window)
	if !errors.Is(err, context.DeadlineExceeded) || time.Since(started) > time.Second {
		t.Fatalf("renewed the remaining sampling budget: %v after %s", err, time.Since(started))
	}
	if fixture.UsageReads.Load() != 1 {
		t.Fatalf("unexpected retry beyond remaining budget: %d", fixture.UsageReads.Load())
	}
	body, err := os.ReadFile(output.Name())
	if err != nil || !strings.Contains(string(body), `"kind":"sample_retry"`) || strings.Contains(string(body), `"pods"`) {
		t.Fatalf("lost failure audit or published an incomplete sample: %s, %v", body, err)
	}
}

func TestObserverSuccessfulHeadersCannotOutliveSamplingGap(t *testing.T) {
	fixture := samplingFixture{Unavailable: 1, BodyDelay: time.Second}
	observer := samplingObserver(t, &fixture)
	output := samplingOutput(t)
	window := samplingWindow{Baseline: &fixture.Baseline, Deadline: time.Now().Add(1150 * time.Millisecond), Output: output}
	started := time.Now()
	_, _, err := observer.sampleWithin(context.Background(), window)
	if !errors.Is(err, context.DeadlineExceeded) || time.Since(started) > 2*time.Second {
		t.Fatalf("accepted response headers without bounded body processing: %v", err)
	}
	body, err := os.ReadFile(output.Name())
	if err != nil || !strings.Contains(string(body), `"kind":"sample_retry"`) || strings.Contains(string(body), `"pods"`) || fixture.UsageReads.Load() != 2 {
		t.Fatalf("retried or published a late response: %s, %v", body, err)
	}
}

func usageResponse(t *testing.T, baseline record, mode string) string {
	t.Helper()
	var usage usageList
	if err := json.Unmarshal(baseline.Usage, &usage); err != nil {
		t.Fatal(err)
	}
	switch mode {
	case "missing-pod":
		usage.Items = usage.Items[1:]
	case "stale":
		usage.Items[0].Timestamp = time.Now().Add(-3 * time.Minute)
	case "stale-on-completion":
		usage.Items[0].Timestamp = time.Now().Add(-2*time.Minute + 100*time.Millisecond)
	case "missing-container":
		usage.Items[0].Containers = nil
	case "missing-memory":
		delete(usage.Items[0].Containers[0].Usage, "memory")
	case "missing-timestamp":
		usage.Items[0].Timestamp = time.Time{}
	case "future-with-missing":
		usage.Items = usage.Items[1:]
		usage.Items[0].Timestamp = time.Now().Add(time.Minute)
	case "invalid-quantity-with-missing":
		usage.Items = usage.Items[1:]
		usage.Items[0].Containers[0].Usage["cpu"] = "not-a-quantity"
	case "duplicate-pod":
		usage.Items = append(usage.Items, usage.Items[0])
	case "duplicate-container":
		usage.Items[0].Containers = append(usage.Items[0].Containers, usage.Items[0].Containers[0])
	}
	body, err := json.Marshal(usage)
	if err != nil {
		t.Fatal(err)
	}
	return string(body)
}

func TestObserverUsageRecoversWithoutPrematureHeartbeat(t *testing.T) {
	for _, mode := range []string{"missing-pod", "stale", "missing-container", "missing-memory", "missing-timestamp", "startup"} {
		t.Run(mode, func(t *testing.T) {
			fixture := samplingFixture{}
			observer := samplingObserver(t, &fixture)
			responseMode := mode
			if mode == "startup" {
				responseMode = "missing-pod"
			}
			fixture.UsageBodies = []string{usageResponse(t, fixture.Baseline, responseMode)}
			output := samplingOutput(t)
			ready := observer.Settings.Output + ".ready"
			oldTime := time.Unix(1, 0)
			if mode != "startup" {
				if err := os.WriteFile(ready, []byte("old"), 0600); err != nil {
					t.Fatal(err)
				}
				if err := os.Chtimes(ready, oldTime, oldTime); err != nil {
					t.Fatal(err)
				}
			}
			window := samplingWindow{Baseline: &fixture.Baseline, Deadline: time.Now().Add(3 * time.Second), Output: output}
			if mode == "startup" {
				window.Baseline = nil
			}
			finished := make(chan error, 1)
			go func() { _, _, err := observer.sampleWithin(context.Background(), window); finished <- err }()
			waitUntil := time.Now().Add(500 * time.Millisecond)
			var body []byte
			for time.Now().Before(waitUntil) {
				body, _ = os.ReadFile(output.Name())
				if strings.Contains(string(body), `"kind":"sample_retry"`) {
					break
				}
				time.Sleep(5 * time.Millisecond)
			}
			if !strings.Contains(string(body), `"kind":"sample_retry"`) || strings.Contains(string(body), `"pods"`) {
				t.Fatalf("missing rejected-attempt audit: %s", body)
			}
			var audit map[string]any
			if err := json.Unmarshal(body, &audit); err != nil {
				t.Fatal(err)
			}
			if audit["reason"] != "usage_unavailable" || audit["attempt_elapsed_ns"].(float64) <= 0 || audit["remaining_ns"].(float64) <= 0 {
				t.Fatalf("incomplete audit: %v", audit)
			}
			issues := audit["issues"].([]any)
			issue := issues[0].(map[string]any)
			if issue["pod"] == "" || issue["pod_uid"] == "" || issue["reason"] == "" {
				t.Fatalf("missing issue identity: %v", issue)
			}
			info, err := os.Stat(ready)
			if mode == "startup" {
				if !errors.Is(err, os.ErrNotExist) {
					t.Fatal("published baseline readiness without a complete sample")
				}
			} else if err != nil || !info.ModTime().Equal(oldTime) {
				t.Fatal("rejected usage refreshed heartbeat")
			}
			if err := <-finished; err != nil {
				t.Fatal(err)
			}
			if fixture.PodReads.Load() != 2 || fixture.JobReads.Load() != 2 || fixture.MetricsReads.Load() != 2 || fixture.UsageReads.Load() != 2 {
				t.Fatal("recovery did not refresh every source")
			}
			body, err = os.ReadFile(ready)
			if err != nil || string(body) != "ready\n" {
				t.Fatal("valid complete sample did not publish readiness")
			}
		})
	}
}

func TestObserverUnavailableUsageCannotRenewDeadline(t *testing.T) {
	for _, mode := range []string{"missing-pod", "stale", "missing-container", "missing-memory", "stale-on-completion"} {
		t.Run(mode, func(t *testing.T) {
			fixture := samplingFixture{}
			observer := samplingObserver(t, &fixture)
			fixture.Body = usageResponse(t, fixture.Baseline, mode)
			budget := 150 * time.Millisecond
			if mode == "stale-on-completion" {
				fixture.BodyDelay = 200 * time.Millisecond
				budget = 350 * time.Millisecond
			}
			output := samplingOutput(t)
			previous := time.Now().Add(-90*time.Second + budget)
			window := samplingWindow{Baseline: &fixture.Baseline, Deadline: previous.Add(90 * time.Second), Output: output}
			_, _, err := observer.sampleWithin(context.Background(), window)
			if !errors.Is(err, context.DeadlineExceeded) {
				t.Fatalf("want original deadline, got %v", err)
			}
			if _, err := os.Stat(observer.Settings.Output + ".ready"); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("expired incomplete sample published a heartbeat")
			}
			body, err := os.ReadFile(output.Name())
			if err != nil || !strings.Contains(string(body), `"kind":"sample_retry"`) || strings.Contains(string(body), `"pods"`) {
				t.Fatalf("invalid expiry audit: %s, %v", body, err)
			}
		})
	}
}

func TestObserverDoesNotRetryHardFailuresAlongsideMissingUsage(t *testing.T) {
	for _, mode := range []string{"401", "403", "500", "malformed", "null-list", "future-with-missing", "invalid-quantity-with-missing", "duplicate-pod", "duplicate-container", "identity", "identity-with-503", "restart", "readiness", "health", "pods-503"} {
		t.Run(mode, func(t *testing.T) {
			fixture := samplingFixture{Body: `{"items":[]}`}
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
			case "null-list":
				fixture.Body = `{"items":null}`
			case "future-with-missing", "invalid-quantity-with-missing", "duplicate-pod", "duplicate-container":
				fixture.Body = usageResponse(t, fixture.Baseline, mode)
			case "identity":
				fixture.BadIdentity = true
			case "identity-with-503":
				fixture.BadIdentity = true
				fixture.Unavailable = 100
			case "restart":
				fixture.BadRestart = true
			case "readiness":
				fixture.BadReadiness = true
			case "health":
				fixture.BadHealth = true
			case "pods-503":
				fixture.PodsUnavailable = true
			}
			output := samplingOutput(t)
			window := samplingWindow{Baseline: &fixture.Baseline, Deadline: time.Now().Add(3 * time.Second), Output: output}
			_, _, err := observer.sampleWithin(context.Background(), window)
			if err == nil || fixture.PodReads.Load() != 1 || fixture.UsageReads.Load() > 1 {
				t.Fatalf("accepted or retried hard failure: %v", err)
			}
			body, readErr := os.ReadFile(output.Name())
			if readErr != nil || len(body) != 0 {
				t.Fatalf("published hard failure as retry/sample: %s, %v", body, readErr)
			}
			if _, err := os.Stat(observer.Settings.Output + ".ready"); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("hard failure published readiness")
			}
		})
	}
}

func TestObserverOutputWriteOverrunCannotPublishHeartbeat(t *testing.T) {
	fixture := samplingFixture{LargeMetrics: true}
	observer := samplingObserver(t, &fixture)
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	defer writer.Close()
	window := samplingWindow{Baseline: &fixture.Baseline, Deadline: time.Now().Add(150 * time.Millisecond), Output: writer}
	finished := make(chan error, 1)
	go func() {
		_, _, err := observer.sampleWithin(context.Background(), window)
		_ = writer.Close()
		finished <- err
	}()
	// A real full pipe delays output past the existing deadline, without a fake
	// clock, file interface or injected function. Releasing it must not publish ready.
	time.Sleep(250 * time.Millisecond)
	written, err := io.Copy(io.Discard, reader)
	if err != nil {
		t.Fatal(err)
	}
	if err := <-finished; !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("accepted late durable output: %v", err)
	}
	if written < 100000 {
		t.Fatalf("output did not exercise a blocked write: %d", written)
	}
	if _, err := os.Stat(observer.Settings.Output + ".ready"); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("late output published heartbeat")
	}
}

func TestObserverExpiredHeartbeatCannotReplacePreviousEvidence(t *testing.T) {
	file := filepath.Join(t.TempDir(), "ready")
	if err := os.WriteFile(file, []byte("previous"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := heartbeat(file, time.Now().Add(-time.Second)); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
	body, err := os.ReadFile(file)
	if err != nil || string(body) != "previous" {
		t.Fatal("expired window replaced readiness")
	}
}

func TestObserverBaselineRejectsExistingLoadReport(t *testing.T) {
	fixture := samplingFixture{}
	observer := samplingObserver(t, &fixture)
	observer.Settings.Report = filepath.Join(t.TempDir(), "existing-load.jsonl")
	if err := os.WriteFile(observer.Settings.Report, []byte("already started"), 0600); err != nil {
		t.Fatal(err)
	}
	output := samplingOutput(t)
	window := samplingWindow{Deadline: time.Now().Add(time.Second), Output: output}
	_, _, err := observer.sampleWithin(context.Background(), window)
	if err == nil || !strings.Contains(err.Error(), "load evidence exists") {
		t.Fatalf("accepted a late baseline: %v", err)
	}
	if _, err := os.Stat(observer.Settings.Output + ".ready"); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("late baseline published readiness")
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
