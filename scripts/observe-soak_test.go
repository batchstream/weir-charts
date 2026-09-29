package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
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
	Baseline   record
	Delay      time.Duration
	Status     int
	Large      bool
	Reads      atomic.Int32
	Unexpected atomic.Int32
}

func (f *samplingFixture) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.Reads.Add(1)
	if f.Status != 0 {
		w.WriteHeader(f.Status)
		return
	}
	switch r.URL.Path {
	case "/api/v1/namespaces/test/pods":
		pods := append(append([]pod(nil), f.Baseline.Pods...), f.Baseline.LoadPods...)
		list := podList{Items: pods}
		if f.Large {
			list.Items[0].Metadata.Labels = map[string]string{"app.kubernetes.io/instance": "weir", "padding": strings.Repeat("x", 200000)}
		}
		_ = json.NewEncoder(w).Encode(list)
	case "/apis/batch/v1/namespaces/test/jobs/load":
		if f.Delay > 0 {
			w.WriteHeader(200)
			w.(http.Flusher).Flush()
			select {
			case <-r.Context().Done():
				return
			case <-time.After(f.Delay):
			}
		}
		_ = json.NewEncoder(w).Encode(f.Baseline.Job)
	default:
		f.Unexpected.Add(1)
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
	token := filepath.Join(t.TempDir(), "task-token")
	if err := os.WriteFile(token, []byte("fake-task-token"), 0600); err != nil {
		t.Fatal(err)
	}
	cfg := settings{Namespace: "test", Job: "load", Release: "weir", Replicas: 1, Output: filepath.Join(t.TempDir(), "observer.jsonl")}
	result := observer{Client: server.Client(), Settings: cfg, APIURL: server.URL, TokenFile: token}
	return result
}
func samplingOutput(t *testing.T) *os.File {
	t.Helper()
	output, err := os.CreateTemp(t.TempDir(), "sample")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = output.Close() })
	return output
}
func TestObserverUsesOnlyLifecycleAPI(t *testing.T) {
	fixture := samplingFixture{}
	observer := samplingObserver(t, &fixture)
	window := samplingWindow{Baseline: &fixture.Baseline, Deadline: time.Now().Add(time.Second), Output: samplingOutput(t)}
	_, _, err := observer.sampleWithin(context.Background(), window)
	if err != nil || fixture.Reads.Load() != 2 || fixture.Unexpected.Load() != 0 {
		t.Fatalf("unexpected collection: %v", err)
	}
	if _, err := os.Stat(observer.Settings.Output + ".ready"); err != nil {
		t.Fatal(err)
	}
}
func TestObserverHTTPFailuresDoNotPublish(t *testing.T) {
	for _, status := range []int{401, 403, 503} {
		fixture := samplingFixture{Status: status}
		observer := samplingObserver(t, &fixture)
		window := samplingWindow{Baseline: &fixture.Baseline, Deadline: time.Now().Add(time.Second), Output: samplingOutput(t)}
		_, _, err := observer.sampleWithin(context.Background(), window)
		if err == nil || fixture.Reads.Load() != 1 {
			t.Fatal("retried or accepted unavailable lifecycle API")
		}
		if _, err := os.Stat(observer.Settings.Output + ".ready"); !errors.Is(err, os.ErrNotExist) {
			t.Fatal("published failed heartbeat")
		}
	}
}
func TestObserverLateHTTPBodyCannotPublish(t *testing.T) {
	fixture := samplingFixture{Delay: 300 * time.Millisecond}
	observer := samplingObserver(t, &fixture)
	window := samplingWindow{Baseline: &fixture.Baseline, Deadline: time.Now().Add(100 * time.Millisecond), Output: samplingOutput(t)}
	_, _, err := observer.sampleWithin(context.Background(), window)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
	if _, err := os.Stat(observer.Settings.Output + ".ready"); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("published late heartbeat")
	}
}
func TestObserverOutputOverrunCannotPublish(t *testing.T) {
	fixture := samplingFixture{Large: true}
	observer := samplingObserver(t, &fixture)
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	defer writer.Close()
	window := samplingWindow{Baseline: &fixture.Baseline, Deadline: time.Now().Add(100 * time.Millisecond), Output: writer}
	done := make(chan error, 1)
	go func() {
		_, _, err := observer.sampleWithin(context.Background(), window)
		_ = writer.Close()
		done <- err
	}()
	time.Sleep(200 * time.Millisecond)
	n, err := io.Copy(io.Discard, reader)
	if err != nil || n < 100000 {
		t.Fatalf("blocked output: %d %v", n, err)
	}
	if err := <-done; !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
	if _, err := os.Stat(observer.Settings.Output + ".ready"); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("published late heartbeat")
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
	ready := containerStatus{Name: "weir", ImageID: "sha256:fixed", ContainerID: "containerd://fixed", Ready: true, Restarts: 0}
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
