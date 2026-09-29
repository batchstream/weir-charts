// observe-soak records one fixed acceptance run using a namespace-scoped ServiceAccount.
package main

import (
	"bufio"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"
)

type metadata struct {
	Name   string            `json:"name"`
	UID    string            `json:"uid"`
	Labels map[string]string `json:"labels"`
}
type containerStatus struct {
	Name        string          `json:"name"`
	ContainerID string          `json:"containerID"`
	Ready       bool            `json:"ready"`
	Restarts    int             `json:"restartCount"`
	ImageID     string          `json:"imageID"`
	State       json.RawMessage `json:"state"`
	LastState   json.RawMessage `json:"lastState"`
}
type podContainerSpec struct {
	Name      string          `json:"name"`
	Resources json.RawMessage `json:"resources"`
}
type podSpec struct {
	NodeName   string             `json:"nodeName"`
	Containers []podContainerSpec `json:"containers"`
}
type podStatus struct {
	Phase      string            `json:"phase"`
	IP         string            `json:"podIP"`
	Containers []containerStatus `json:"containerStatuses"`
}
type pod struct {
	Metadata metadata  `json:"metadata"`
	Spec     podSpec   `json:"spec"`
	Status   podStatus `json:"status"`
}
type podList struct {
	Items []pod `json:"items"`
}
type jobStatus struct {
	Active    int `json:"active"`
	Failed    int `json:"failed"`
	Succeeded int `json:"succeeded"`
}
type job struct {
	Metadata metadata  `json:"metadata"`
	Status   jobStatus `json:"status"`
}
type record struct {
	UTC      time.Time `json:"utc"`
	Pods     []pod     `json:"pods"`
	LoadPods []pod     `json:"loadPods"`
	Job      job       `json:"job"`
}

type settings struct {
	Namespace       string        `json:"namespace"`
	RunConfigSHA256 string        `json:"runConfigSHA256"`
	Job             string        `json:"job"`
	Release         string        `json:"release"`
	Output          string        `json:"output"`
	Interval        time.Duration `json:"interval"`
	Duration        time.Duration `json:"duration"`
	Replicas        int           `json:"replicas"`
	Report          string        `json:"report"`
	ExitStatus      string        `json:"exitStatus"`
	RunID           string        `json:"runID"`
	LoadDuration    time.Duration `json:"loadDuration"`
}
type observer struct {
	Client    *http.Client
	Settings  settings
	APIURL    string
	TokenFile string
}

type apiStatusError struct {
	Path   string
	Status int
}

func (e *apiStatusError) Error() string {
	return fmt.Sprintf("Kubernetes read %s returned HTTP %d", e.Path, e.Status)
}

type samplingWindow struct {
	Baseline *record
	Deadline time.Time
	Output   *os.File
}

const account = "/var/run/secrets/kubernetes.io/serviceaccount"

func (o *observer) get(ctx context.Context, path string, target any) error {
	// This newly projected task ServiceAccount token rotates during a 24-hour run.
	// Read it only inside the observer; never record it or grant Secret permissions.
	token, err := os.ReadFile(o.TokenFile)
	if err != nil || len(token) > 16384 {
		return errors.New("projected task token unavailable")
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, o.APIURL+path, nil)
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+strings.TrimSpace(string(token)))
	response, err := o.Client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		failure := &apiStatusError{Path: path, Status: response.StatusCode}
		return failure
	}
	decoder := json.NewDecoder(io.LimitReader(response.Body, 8<<20))
	return decoder.Decode(target)
}

func (o *observer) sample(ctx context.Context) (record, error) {
	result := record{UTC: time.Now().UTC()}
	var pods podList
	if err := o.get(ctx, "/api/v1/namespaces/"+o.Settings.Namespace+"/pods", &pods); err != nil {
		return result, err
	}
	for _, item := range pods.Items {
		labels := item.Metadata.Labels
		if labels["job-name"] == o.Settings.Job || labels["batch.kubernetes.io/job-name"] == o.Settings.Job {
			result.LoadPods = append(result.LoadPods, item)
		}
		weir := labels["app.kubernetes.io/instance"] == o.Settings.Release
		if !weir && labels["app"] != "mongo" && labels["app"] != "elasticsearch" {
			continue
		}
		result.Pods = append(result.Pods, item)
	}
	if err := o.get(ctx, "/apis/batch/v1/namespaces/"+o.Settings.Namespace+"/jobs/"+o.Settings.Job, &result.Job); err != nil {
		return result, err
	}
	return result, nil
}

// sampleWithin keeps the original monotonic deadline across reads and durable
// publication. Prometheus evidence is independently audited, never queried here.
func (o *observer) sampleWithin(ctx context.Context, window samplingWindow) (record, time.Time, error) {
	ctx, cancel := context.WithDeadline(ctx, window.Deadline)
	defer cancel()
	sampleTime := time.Now()
	current, err := o.sample(ctx)
	if err != nil {
		return current, sampleTime, err
	}
	baseline := current
	if window.Baseline != nil {
		baseline = *window.Baseline
	}
	if err := validate(current, baseline, o.Settings.Replicas); err != nil {
		return current, sampleTime, err
	}
	if window.Baseline == nil {
		if err := baselineRunning(current); err != nil {
			return current, sampleTime, err
		}
		for _, path := range []string{o.Settings.Report, o.Settings.ExitStatus} {
			if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
				return current, sampleTime, errors.New("load evidence exists before observer readiness")
			}
		}
	}
	if !time.Now().Before(window.Deadline) {
		return current, sampleTime, context.DeadlineExceeded
	}
	current.UTC = time.Now().UTC()
	if err := json.NewEncoder(window.Output).Encode(current); err != nil {
		return current, sampleTime, err
	}
	if !time.Now().Before(window.Deadline) {
		return current, sampleTime, context.DeadlineExceeded
	}
	if err := window.Output.Sync(); err != nil {
		return current, sampleTime, err
	}
	if err := ctx.Err(); err != nil {
		return current, sampleTime, err
	}
	if err := heartbeat(o.Settings.Output+".ready", window.Deadline); err != nil {
		return current, sampleTime, err
	}
	return current, sampleTime, ctx.Err()
}

func sameContainers(current, baseline pod) error {
	if len(current.Status.Containers) == 0 || len(current.Status.Containers) != len(baseline.Status.Containers) {
		return errors.New("container identity count changed")
	}
	identities := make(map[string]containerStatus)
	for _, container := range baseline.Status.Containers {
		identities[container.Name] = container
	}
	for _, container := range current.Status.Containers {
		if container.ImageID == "" || container.ContainerID == "" || identities[container.Name].ImageID != container.ImageID || identities[container.Name].ContainerID != container.ContainerID || container.Restarts != 0 {
			return errors.New("container image identity changed or restarted")
		}
	}
	return nil
}

func validate(current, baseline record, replicas int) error {
	if len(current.Pods) != replicas+2 || len(current.LoadPods) != 1 || len(baseline.LoadPods) != 1 {
		return errors.New("unexpected acceptance Pod count")
	}
	if current.Job.Metadata.UID != baseline.Job.Metadata.UID || current.LoadPods[0].Metadata.UID != baseline.LoadPods[0].Metadata.UID {
		return errors.New("load Job or Pod identity changed")
	}
	identities := make(map[string]pod)
	for _, item := range baseline.Pods {
		identities[item.Metadata.UID] = item
	}
	for _, item := range current.Pods {
		original, ok := identities[item.Metadata.UID]
		if !ok || item.Metadata.Name != original.Metadata.Name || item.Spec.NodeName != original.Spec.NodeName || item.Status.IP != original.Status.IP || item.Status.Phase != "Running" {
			return fmt.Errorf("service Pod identity or lifecycle changed: %s", item.Metadata.Name)
		}
		if err := sameContainers(item, original); err != nil {
			return err
		}
		for _, container := range item.Status.Containers {
			if !container.Ready {
				return fmt.Errorf("service Pod lost readiness: %s", item.Metadata.Name)
			}
		}
	}
	load := current.LoadPods[0]
	if load.Status.Phase != "Running" && load.Status.Phase != "Succeeded" {
		return errors.New("load Pod left its running lifecycle")
	}
	if err := sameContainers(load, baseline.LoadPods[0]); err != nil {
		return err
	}
	if load.Status.Phase == "Succeeded" {
		for _, container := range load.Status.Containers {
			var state struct {
				Terminated *struct {
					ExitCode *int `json:"exitCode"`
				} `json:"terminated"`
			}
			if err := json.Unmarshal(container.State, &state); err != nil || state.Terminated == nil || state.Terminated.ExitCode == nil || *state.Terminated.ExitCode != 0 {
				return errors.New("load Pod lacks successful exit evidence")
			}
		}
	}
	if current.Job.Status.Failed != 0 {
		return errors.New("load Job failed")
	}
	return nil
}

func baselineRunning(current record) error {
	if current.Job.Status.Active != 1 || current.Job.Status.Succeeded != 0 || current.Job.Status.Failed != 0 || len(current.LoadPods) != 1 || current.LoadPods[0].Status.Phase != "Running" {
		return errors.New("observer must start before the active load Job runs")
	}
	for _, container := range current.LoadPods[0].Status.Containers {
		if !container.Ready {
			return errors.New("load container is not ready for the observer baseline")
		}
	}
	return nil
}

func exclusiveResult(path string, body []byte) error {
	temp, err := os.OpenFile(path+".tmp", os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0644)
	if err != nil {
		return err
	}
	defer os.Remove(temp.Name())
	if _, err := temp.Write(body); err != nil {
		temp.Close()
		return err
	}
	if err := temp.Sync(); err != nil {
		temp.Close()
		return err
	}
	if err := temp.Close(); err != nil {
		return err
	}
	if err := os.Link(temp.Name(), path); err != nil {
		return err
	}
	return syncDirectory(path)
}

func syncDirectory(path string) error {
	directory, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer directory.Close()
	return directory.Sync()
}

func heartbeat(path string, deadline time.Time) error {
	if !time.Now().Before(deadline) {
		return context.DeadlineExceeded
	}
	temp, err := os.CreateTemp(filepath.Dir(path), "heartbeat-")
	if err != nil {
		return err
	}
	defer os.Remove(temp.Name())
	if _, err := temp.WriteString("ready\n"); err != nil {
		temp.Close()
		return err
	}
	if err := temp.Sync(); err != nil {
		temp.Close()
		return err
	}
	if err := temp.Close(); err != nil {
		return err
	}
	if !time.Now().Before(deadline) {
		return context.DeadlineExceeded
	}
	if err := os.Rename(temp.Name(), path); err != nil {
		return err
	}
	if err := syncDirectory(path); err != nil {
		return err
	}
	if !time.Now().Before(deadline) {
		return context.DeadlineExceeded
	}
	return nil
}

func verifyLoad(cfg settings) error {
	raw, err := os.ReadFile(cfg.ExitStatus)
	if err != nil {
		return err
	}
	var status struct {
		ExitCode *int `json:"exit_code"`
	}
	if err := json.Unmarshal(raw, &status); err != nil || status.ExitCode == nil || *status.ExitCode != 0 {
		return errors.New("load exit status is not zero")
	}
	file, err := os.Open(cfg.Report)
	if err != nil {
		return err
	}
	defer file.Close()
	stat, err := file.Stat()
	if err != nil {
		return err
	}
	if stat.Size() > 16<<20 {
		return errors.New("load report exceeds bound")
	}
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 4096), 256<<10)
	var last map[string]json.RawMessage
	for scanner.Scan() {
		var entry map[string]json.RawMessage
		if err := json.Unmarshal(scanner.Bytes(), &entry); err != nil {
			return errors.New("load report contains invalid JSONL")
		}
		last = entry
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	var kind, runID string
	var elapsed int64
	var failures, unknown *uint64
	if err := json.Unmarshal(last["kind"], &kind); err != nil {
		return err
	}
	if err := json.Unmarshal(last["run_id"], &runID); err != nil {
		return err
	}
	if err := json.Unmarshal(last["elapsed_ns"], &elapsed); err != nil {
		return err
	}
	if err := json.Unmarshal(last["failures"], &failures); err != nil {
		return err
	}
	if err := json.Unmarshal(last["unknown"], &unknown); err != nil {
		return err
	}
	if kind != "passed" || runID != cfg.RunID || elapsed < int64(cfg.LoadDuration) || failures == nil || unknown == nil || *failures != 0 || *unknown != 0 {
		return errors.New("load report does not prove the fixed run passed")
	}
	return nil
}

func (o *observer) run(ctx context.Context) error {
	output, err := os.OpenFile(o.Settings.Output, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0644)
	if err != nil {
		return err
	}
	defer output.Close()
	encoder := json.NewEncoder(output)
	if err := encoder.Encode(o.Settings); err != nil {
		return err
	}
	window := samplingWindow{Deadline: time.Now().Add(30 * time.Second), Output: output}
	baseline, sampleTime, err := o.sampleWithin(ctx, window)
	if err != nil {
		return err
	}
	current := baseline
	deadline := sampleTime.Add(o.Settings.Duration)
	for {
		if current.Job.Status.Succeeded == 1 && current.LoadPods[0].Status.Phase == "Succeeded" {
			return verifyLoad(o.Settings)
		}
		if !time.Now().Before(deadline) {
			return errors.New("observer deadline reached before load completion")
		}
		timer := time.NewTimer(time.Until(sampleTime.Add(o.Settings.Interval)))
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		}
		nextDeadline := sampleTime.Add(o.Settings.Interval + 30*time.Second)
		if deadline.Before(nextDeadline) {
			nextDeadline = deadline
		}
		window = samplingWindow{Baseline: &baseline, Deadline: nextDeadline, Output: output}
		current, sampleTime, err = o.sampleWithin(ctx, window)
		if err != nil {
			return err
		}
	}
}

func main() {
	cfg := settings{}
	flag.StringVar(&cfg.Namespace, "namespace", "", "owned namespace")
	flag.StringVar(&cfg.RunConfigSHA256, "run-config-sha256", "", "canonical frozen run configuration SHA256")
	flag.StringVar(&cfg.Job, "job", "", "load Job name")
	flag.StringVar(&cfg.Release, "release", "weir", "Helm release")
	flag.StringVar(&cfg.Output, "output", "/results/observations.jsonl", "new persistent output file")
	flag.DurationVar(&cfg.Interval, "interval", time.Minute, "frozen observation interval")
	flag.DurationVar(&cfg.Duration, "duration", 25*time.Hour, "overall observer deadline")
	flag.IntVar(&cfg.Replicas, "replicas", 3, "expected Weir replica count")
	flag.StringVar(&cfg.Report, "report", "/results/load.jsonl", "load JSONL report")
	flag.StringVar(&cfg.ExitStatus, "exit-status", "/results/load-status.json", "load exit status")
	flag.StringVar(&cfg.RunID, "run-id", "", "fixed load run ID")
	flag.DurationVar(&cfg.LoadDuration, "load-duration", 24*time.Hour, "required load duration")
	flag.Parse()
	if cfg.RunID == "" {
		cfg.RunID = cfg.Job
	}
	name := regexp.MustCompile(`^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$`)
	if !regexp.MustCompile(`^[a-f0-9]{64}$`).MatchString(cfg.RunConfigSHA256) || !name.MatchString(cfg.Namespace) || !name.MatchString(cfg.Job) || cfg.Interval < time.Second || cfg.Duration < cfg.Interval || cfg.LoadDuration <= 0 || cfg.Replicas < 1 {
		fmt.Fprintln(os.Stderr, "invalid observer configuration")
		os.Exit(1)
	}
	ca, err := os.ReadFile(filepath.Join(account, "ca.crt"))
	if err != nil {
		fmt.Fprintln(os.Stderr, "task API CA unavailable")
		os.Exit(1)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(ca) {
		fmt.Fprintln(os.Stderr, "task API CA invalid")
		os.Exit(1)
	}
	tlsConfig := &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: roots}
	transport := &http.Transport{TLSClientConfig: tlsConfig}
	client := &http.Client{Transport: transport, Timeout: 10 * time.Second}
	runner := observer{Client: client, Settings: cfg, APIURL: "https://kubernetes.default.svc", TokenFile: filepath.Join(account, "token")}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	err = runner.run(ctx)
	status := map[string]any{"completed_at": time.Now().UTC(), "passed": err == nil, "runID": cfg.RunID, "namespace": cfg.Namespace, "job": cfg.Job, "runConfigSHA256": cfg.RunConfigSHA256}
	if err != nil {
		status["error"] = err.Error()
	}
	body, _ := json.Marshal(status)
	if writeErr := exclusiveResult(cfg.Output+".status.json", body); writeErr != nil {
		fmt.Fprintln(os.Stderr, "observer result persistence failed")
		os.Exit(1)
	}
	fmt.Println(string(body))
	if err != nil {
		os.Exit(1)
	}
}
