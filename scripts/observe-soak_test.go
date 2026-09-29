package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

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
	result := record{Pods: []pod{first, second, third}, LoadPods: []pod{load}, Job: runningJob}
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
