package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type SenderConfig struct {
	AccountIndex         int               `json:"account_index"`
	BuyURL               string            `json:"buy_url"`
	Headers              map[string]string `json:"headers"`
	Payload              map[string]any    `json:"payload"`
	PrewarmMs            int64             `json:"prewarm_ms"`
	FormalMs             int64             `json:"formal_ms"`
	ActiveEndMs          int64             `json:"active_end_ms"`
	HardStopMs           int64             `json:"hard_stop_ms"`
	PrewarmConcurrency   int               `json:"prewarm_concurrency"`
	BurstConcurrency     int               `json:"burst_concurrency"`
	BurstIntervalMs      int               `json:"burst_interval_ms"`
	MaxInflight          int               `json:"max_inflight"`
	RequestTimeoutMs     int               `json:"request_timeout_ms"`
	DrainGraceMs         int               `json:"drain_grace_ms"`
	ResponseLogLimit     int               `json:"response_log_limit"`
	ResponseLogEvery     int               `json:"response_log_every"`
	ResponseLogBodyChars int               `json:"response_log_body_chars"`
	DisableHTTP2         bool              `json:"disable_http2"`
}

type SenderResult struct {
	AttemptsSent        int64          `json:"attempts_sent"`
	Success             bool           `json:"success"`
	SuccessSentAt       string         `json:"success_sent_at"`
	SuccessReceivedAt   string         `json:"success_received_at"`
	FirstResponse       any            `json:"first_response"`
	First401Response    any            `json:"first_401_response"`
	FirstNon401Response any            `json:"first_non_401_response"`
	SuccessResponse     any            `json:"success_response"`
	LastResponseMessage string         `json:"last_response_message"`
	ResponseCounts      map[string]int `json:"response_counts"`
	SkippedByCapacity   int64          `json:"skipped_by_capacity"`
}

type senderState struct {
	cfg                 SenderConfig
	client              *http.Client
	payload             []byte
	stop                atomic.Bool
	attempts            atomic.Int64
	skippedByCapacity   atomic.Int64
	logsEmitted         int
	firstResponse       any
	first401Response    any
	firstNon401Response any
	successResponse     any
	successSentAt       string
	successReceivedAt   string
	lastResponseMessage string
	responseCounts      map[string]int
	mu                  sync.Mutex
}

func nowMs() int64 {
	return time.Now().UnixNano() / int64(time.Millisecond)
}

func formatMs(ms int64) string {
	return time.Unix(0, ms*int64(time.Millisecond)).Format("15:04:05.000")
}

func logf(format string, args ...any) {
	fmt.Printf("[%s] %s\n", time.Now().Format("15:04:05.000"), fmt.Sprintf(format, args...))
}

func waitUntilMs(target int64) {
	for {
		remaining := target - nowMs()
		if remaining <= 0 {
			return
		}
		switch {
		case remaining > 60000:
			time.Sleep(60 * time.Second)
		case remaining > 1000:
			time.Sleep(time.Duration(remaining-500) * time.Millisecond)
		default:
			time.Sleep(time.Duration(remaining) * time.Millisecond)
		}
	}
}

func safeMessage(value any) string {
	obj, ok := value.(map[string]any)
	if !ok {
		return fmt.Sprint(value)
	}
	for _, key := range []string{"message", "msg", "errorMessage", "error"} {
		if v, exists := obj[key]; exists && v != nil && fmt.Sprint(v) != "" {
			return fmt.Sprint(v)
		}
	}
	raw, err := json.Marshal(obj)
	if err != nil {
		return fmt.Sprint(obj)
	}
	return string(raw)
}

func jsonPreview(value any, fallback string, limit int) string {
	if limit <= 0 {
		limit = 500
	}
	var text string
	if value != nil {
		if raw, err := json.Marshal(value); err == nil {
			text = string(raw)
		} else {
			text = fmt.Sprint(value)
		}
	} else {
		text = fallback
	}
	text = strings.Join(strings.Fields(text), " ")
	if len(text) <= limit {
		return text
	}
	return text[:limit] + fmt.Sprintf("...(truncated,len=%d)", len(text))
}

func responseFingerprint(data any, httpStatus int) string {
	if obj, ok := data.(map[string]any); ok {
		msg := safeMessage(obj)
		if len(msg) > 160 {
			msg = msg[:160]
		}
		return fmt.Sprintf("http=%d code=%v success=%v msg=%s", httpStatus, obj["code"], obj["success"], msg)
	}
	return fmt.Sprintf("http=%d non-json", httpStatus)
}

func isSuccess(data any) bool {
	obj, ok := data.(map[string]any)
	if !ok {
		return false
	}
	success, _ := obj["success"].(bool)
	if !success {
		return false
	}
	switch code := obj["code"].(type) {
	case float64:
		return int(code) == 200
	case int:
		return code == 200
	case string:
		return code == "200"
	default:
		return false
	}
}

func isLocalTransportError(data any, httpStatus int) bool {
	if httpStatus != 0 {
		return false
	}
	obj, ok := data.(map[string]any)
	if !ok {
		return false
	}
	code := fmt.Sprint(obj["code"])
	success := fmt.Sprint(obj["success"])
	return code == "0" && success == "false"
}

func isUnauthorizedResponse(data any, httpStatus int) bool {
	if httpStatus == http.StatusUnauthorized {
		return true
	}
	obj, ok := data.(map[string]any)
	if !ok {
		return false
	}
	switch code := obj["code"].(type) {
	case float64:
		if int(code) == http.StatusUnauthorized {
			return true
		}
	case int:
		if code == http.StatusUnauthorized {
			return true
		}
	case string:
		if code == "401" {
			return true
		}
	}
	message := safeMessage(obj)
	return strings.Contains(message, "未登录") || strings.Contains(message, "会话失效")
}

func setHeaders(req *http.Request, headers map[string]string) {
	for name, value := range headers {
		key := strings.ToLower(strings.TrimSpace(name))
		if key == "" || key == "content-length" || key == "accept-encoding" || strings.HasPrefix(key, ":") {
			continue
		}
		req.Header.Set(name, value)
	}
}

func (s *senderState) sendOnce(ctx context.Context) {
	if s.stop.Load() {
		return
	}
	attempt := s.attempts.Add(1)
	start := nowMs()

	timeoutMs := s.cfg.RequestTimeoutMs
	if timeoutMs <= 0 {
		timeoutMs = 12000
	}
	reqCtx, cancel := context.WithTimeout(ctx, time.Duration(timeoutMs)*time.Millisecond)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodPost, s.cfg.BuyURL, bytes.NewReader(s.payload))
	if err != nil {
		s.record(attempt, start, nowMs(), 0, map[string]any{"success": false, "code": 0, "message": err.Error()}, "")
		return
	}
	setHeaders(req, s.cfg.Headers)
	req.Header.Set("Content-Type", "application/json")

	resp, err := s.client.Do(req)
	end := nowMs()
	if err != nil {
		s.record(attempt, start, end, 0, map[string]any{"success": false, "code": 0, "message": err.Error()}, "")
		return
	}
	defer resp.Body.Close()

	bodyBytes, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	body := string(bodyBytes)
	if readErr != nil {
		s.record(attempt, start, nowMs(), resp.StatusCode, map[string]any{"success": false, "code": 0, "message": readErr.Error()}, body)
		return
	}

	var data any
	if err := json.Unmarshal(bodyBytes, &data); err != nil {
		data = map[string]any{"success": false, "code": resp.StatusCode, "message": body}
	}
	s.record(attempt, start, end, resp.StatusCode, data, body)
}

func (s *senderState) record(attempt, start, end int64, httpStatus int, data any, rawBody string) {
	success := isSuccess(data)
	fingerprint := responseFingerprint(data, httpStatus)
	shouldLog := success

	s.mu.Lock()
	s.responseCounts[fingerprint]++
	if s.firstResponse == nil {
		s.firstResponse = data
		shouldLog = true
	}
	if isUnauthorizedResponse(data, httpStatus) {
		if s.first401Response == nil {
			s.first401Response = data
		}
	} else if httpStatus > 0 && s.firstNon401Response == nil {
		s.firstNon401Response = data
	}
	if data != nil && (!isLocalTransportError(data, httpStatus) || s.lastResponseMessage == "") {
		s.lastResponseMessage = safeMessage(data)
	}
	if s.cfg.ResponseLogLimit < 0 {
		s.cfg.ResponseLogLimit = 0
	}
	if s.logsEmitted < s.cfg.ResponseLogLimit {
		shouldLog = true
	}
	if s.cfg.ResponseLogEvery > 0 && attempt%int64(s.cfg.ResponseLogEvery) == 0 {
		shouldLog = true
	}
	if shouldLog {
		s.logsEmitted++
	}
	if success && !s.stop.Load() {
		s.successResponse = data
		s.successSentAt = time.Unix(0, start*int64(time.Millisecond)).Format(time.RFC3339Nano)
		s.successReceivedAt = time.Unix(0, end*int64(time.Millisecond)).Format(time.RFC3339Nano)
		s.stop.Store(true)
	}
	s.mu.Unlock()

	if shouldLog {
		logf(
			"account %d: go response #%d sent=%s recv=%s rtt=%.1fms http=%d body=%s",
			s.cfg.AccountIndex,
			attempt,
			formatMs(start),
			formatMs(end),
			float64(end-start),
			httpStatus,
			jsonPreview(data, rawBody, s.cfg.ResponseLogBodyChars),
		)
	}
}

func (s *senderState) fire(ctx context.Context, count int, sem chan struct{}, wg *sync.WaitGroup) int {
	fired := 0
	for i := 0; i < count; i++ {
		if s.stop.Load() || ctx.Err() != nil {
			return fired
		}
		select {
		case sem <- struct{}{}:
			fired++
		default:
			s.skippedByCapacity.Add(int64(count - i))
			return fired
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() { <-sem }()
			s.sendOnce(ctx)
		}()
	}
	return fired
}

func (s *senderState) run() SenderResult {
	if s.cfg.PrewarmConcurrency <= 0 {
		s.cfg.PrewarmConcurrency = 30
	}
	if s.cfg.BurstConcurrency <= 0 {
		s.cfg.BurstConcurrency = 120
	}
	if s.cfg.BurstIntervalMs <= 0 {
		s.cfg.BurstIntervalMs = 10
	}
	if s.cfg.RequestTimeoutMs <= 0 {
		s.cfg.RequestTimeoutMs = 12000
	}
	if s.cfg.DrainGraceMs <= 0 {
		s.cfg.DrainGraceMs = 1000
	}
	if s.cfg.ResponseLogBodyChars <= 0 {
		s.cfg.ResponseLogBodyChars = 500
	}

	ctx, cancel := context.WithCancel(context.Background())
	if s.cfg.HardStopMs > 0 {
		delay := time.Duration(s.cfg.HardStopMs-nowMs()) * time.Millisecond
		if delay > 0 {
			time.AfterFunc(delay, cancel)
		}
	}
	defer cancel()

	maxWorkers := s.cfg.MaxInflight
	if maxWorkers <= 0 {
		maxWorkers = s.cfg.BurstConcurrency * 4
	}
	if s.cfg.PrewarmConcurrency > maxWorkers {
		maxWorkers = s.cfg.PrewarmConcurrency
	}
	if maxWorkers < 1 {
		maxWorkers = 1
	}
	sem := make(chan struct{}, maxWorkers)
	var wg sync.WaitGroup

	waitUntilMs(s.cfg.PrewarmMs)
	if ctx.Err() == nil && !s.stop.Load() {
		logf("account %d: go prewarm %d concurrent requests", s.cfg.AccountIndex, s.cfg.PrewarmConcurrency)
		s.fire(ctx, s.cfg.PrewarmConcurrency, sem, &wg)
	}

	waitUntilMs(s.cfg.FormalMs)
	nextRound := s.cfg.FormalMs
	rounds := 0
	for nowMs() <= s.cfg.ActiveEndMs && nowMs() < s.cfg.HardStopMs && !s.stop.Load() && ctx.Err() == nil {
		waitUntilMs(nextRound)
		if s.stop.Load() || ctx.Err() != nil || nowMs() >= s.cfg.HardStopMs {
			break
		}
		rounds++
		fired := s.fire(ctx, s.cfg.BurstConcurrency, sem, &wg)
		if fired == 0 && rounds%25 == 0 {
			logf(
				"account %d: go send capacity full rounds=%d attempts=%d skipped=%d",
				s.cfg.AccountIndex,
				rounds,
				s.attempts.Load(),
				s.skippedByCapacity.Load(),
			)
		}
		nextRound += int64(s.cfg.BurstIntervalMs)
	}

	logf("account %d: go send loop ended rounds=%d attempts=%d success=%v", s.cfg.AccountIndex, rounds, s.attempts.Load(), s.successResponse != nil)

	drainUntil := nowMs() + int64(s.cfg.RequestTimeoutMs+s.cfg.DrainGraceMs)
	if s.cfg.HardStopMs > 0 && s.cfg.HardStopMs < drainUntil {
		drainUntil = s.cfg.HardStopMs
	}
	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()
	if s.stop.Load() {
		cancel()
		<-done
	} else {
		waitMs := drainUntil - nowMs()
		if waitMs > 0 {
			select {
			case <-done:
			case <-time.After(time.Duration(waitMs) * time.Millisecond):
				cancel()
				<-done
			}
		} else {
			cancel()
			<-done
		}
	}
	s.client.CloseIdleConnections()

	s.mu.Lock()
	defer s.mu.Unlock()
	return SenderResult{
		AttemptsSent:        s.attempts.Load(),
		Success:             s.successResponse != nil,
		SuccessSentAt:       s.successSentAt,
		SuccessReceivedAt:   s.successReceivedAt,
		FirstResponse:       s.firstResponse,
		First401Response:    s.first401Response,
		FirstNon401Response: s.firstNon401Response,
		SuccessResponse:     s.successResponse,
		LastResponseMessage: s.lastResponseMessage,
		ResponseCounts:      s.responseCounts,
		SkippedByCapacity:   s.skippedByCapacity.Load(),
	}
}

func loadConfig(path string) (SenderConfig, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return SenderConfig{}, err
	}
	var cfg SenderConfig
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return SenderConfig{}, err
	}
	return cfg, nil
}

func writeResult(path string, result SenderResult) error {
	raw, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		return err
	}
	if path == "" {
		fmt.Println(string(raw))
		return nil
	}
	return os.WriteFile(path, raw, 0600)
}

func printSummary(counts map[string]int) {
	type item struct {
		Key   string
		Count int
	}
	items := make([]item, 0, len(counts))
	for key, count := range counts {
		items = append(items, item{Key: key, Count: count})
	}
	sort.Slice(items, func(i, j int) bool {
		if items[i].Count != items[j].Count {
			return items[i].Count > items[j].Count
		}
		return items[i].Key < items[j].Key
	})
	limit := len(items)
	if limit > 8 {
		limit = 8
	}
	if limit == 0 {
		return
	}
	parts := make([]string, 0, limit)
	for _, item := range items[:limit] {
		parts = append(parts, fmt.Sprintf("%dx %s", item.Count, item.Key))
	}
	logf("go response summary %s", strings.Join(parts, " | "))
}

func main() {
	configPath := flag.String("config", "", "sender config json path")
	outputPath := flag.String("output", "", "sender result json path")
	flag.Parse()
	if *configPath == "" {
		fmt.Fprintln(os.Stderr, "missing -config")
		os.Exit(2)
	}

	cfg, err := loadConfig(*configPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "load config: %v\n", err)
		os.Exit(2)
	}
	payload, err := json.Marshal(cfg.Payload)
	if err != nil {
		fmt.Fprintf(os.Stderr, "marshal payload: %v\n", err)
		os.Exit(2)
	}

	connLimit := cfg.MaxInflight
	if connLimit <= 0 {
		connLimit = cfg.BurstConcurrency * 4
	}
	if connLimit < cfg.BurstConcurrency {
		connLimit = cfg.BurstConcurrency
	}
	if connLimit < cfg.PrewarmConcurrency {
		connLimit = cfg.PrewarmConcurrency
	}
	if connLimit < 1 {
		connLimit = 120
	}
	transport := &http.Transport{
		ForceAttemptHTTP2:     !cfg.DisableHTTP2,
		MaxIdleConns:          connLimit + 20,
		MaxIdleConnsPerHost:   connLimit,
		MaxConnsPerHost:       connLimit,
		IdleConnTimeout:       30 * time.Second,
		TLSHandshakeTimeout:   5 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
	}
	client := &http.Client{Transport: transport, Timeout: 0}

	state := &senderState{
		cfg:            cfg,
		client:         client,
		payload:        payload,
		responseCounts: make(map[string]int),
	}
	result := state.run()
	printSummary(result.ResponseCounts)
	if err := writeResult(*outputPath, result); err != nil {
		fmt.Fprintf(os.Stderr, "write result: %v\n", err)
		os.Exit(1)
	}
	if result.Success {
		os.Exit(0)
	}
	os.Exit(0)
}
