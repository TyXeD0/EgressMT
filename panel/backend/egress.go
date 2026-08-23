package server

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os/exec"
	"strconv"
	"strings"
	"time"

	"github.com/Liafanx/mtproxyl-panel/internal/auth"
)

const egressBridge = "/usr/local/sbin/mtproxyl-egress-panel-bridge"

func runEgressBridge(parent context.Context, args ...string) ([]byte, error) {
	return runEgressBridgeInput(parent, nil, args...)
}

func runEgressBridgeInput(parent context.Context, input []byte, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(parent, 25*time.Second)
	defer cancel()
	full := append([]string{"-n", egressBridge}, args...)
	cmd := exec.CommandContext(ctx, "sudo", full...)
	cmd.Env = []string{"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
	if input != nil { cmd.Stdin = bytes.NewReader(input) }
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout; cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		if ctx.Err() == context.DeadlineExceeded { return nil, fmt.Errorf("EgressMT command timed out") }
		msg := strings.TrimSpace(stderr.String())
		if msg == "" { msg = strings.TrimSpace(stdout.String()) }
		if msg == "" { msg = err.Error() }
		return nil, fmt.Errorf("EgressMT: %s", msg)
	}
	return stdout.Bytes(), nil
}

func writeEgressJSON(w http.ResponseWriter, raw []byte) {
	var data any
	if err := json.Unmarshal(raw, &data); err != nil {
		writeError(w, http.StatusBadGateway, "egress_invalid_json", "Некорректный ответ EgressMT")
		return
	}
	writeJSON(w, http.StatusOK, jsonResponse{OK: true, Data: data})
}

func validNodeRef(ref string) bool {
	ref = strings.TrimSpace(ref)
	return ref != "" && len(ref) <= 128 && !strings.ContainsAny(ref, "\r\n\x00")
}
func validJobID(job string) bool {
	if len(job) != 18 || !strings.HasPrefix(job, "j-") { return false }
	for _, r := range job[2:] {
		if !((r >= '0' && r <= '9') || (r >= 'a' && r <= 'f')) { return false }
	}
	return true
}

type egressSSHAuthRequest struct { Mode string `json:"mode"`; Secret string `json:"secret,omitempty"` }
func validateEgressSSHAuth(a egressSSHAuthRequest) error {
	switch a.Mode {
	case "auto": return nil
	case "password", "key":
		if a.Secret == "" { return fmt.Errorf("для выбранного способа SSH требуется пароль или private key") }
		if len(a.Secret) > 64<<10 || strings.ContainsRune(a.Secret, '\x00') { return fmt.Errorf("некорректные SSH credentials") }
		return nil
	default: return fmt.Errorf("SSH auth mode: auto, password или key")
	}
}

func (s *Server) registerEgressRoutes(mux *http.ServeMux, jwtSecret []byte) {
	protected := func(h http.HandlerFunc) http.Handler { return auth.RequireAuth(jwtSecret, h) }

	mux.Handle("GET /api/egress/status", protected(func(w http.ResponseWriter, r *http.Request) {
		out, err := runEgressBridge(r.Context(), "status")
		if err != nil { writeError(w, http.StatusServiceUnavailable, "egress_status_failed", err.Error()); return }
		writeEgressJSON(w, out)
	}))

	mux.Handle("POST /api/egress/mode", protected(func(w http.ResponseWriter, r *http.Request) {
		var req struct { Mode string `json:"mode"`; Node string `json:"node"` }
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<10)).Decode(&req); err != nil {
			writeError(w, http.StatusBadRequest, "bad_request", `Ожидается {"mode":"auto"|"direct"|"block"|"manual","node":"..."}`); return
		}
		switch req.Mode {
		case "auto", "direct", "block":
			if _, err := runEgressBridge(r.Context(), "mode", req.Mode); err != nil { writeError(w, http.StatusBadGateway, "egress_mode_failed", err.Error()); return }
		case "manual":
			if !validNodeRef(req.Node) { writeError(w, http.StatusBadRequest, "invalid_node", "Для manual требуется корректная нода"); return }
			if _, err := runEgressBridge(r.Context(), "mode", "manual", req.Node); err != nil { writeError(w, http.StatusBadGateway, "egress_mode_failed", err.Error()); return }
		default: writeError(w, http.StatusBadRequest, "invalid_mode", "Режим: auto, direct, block или manual"); return
		}
		out, err := runEgressBridge(r.Context(), "status")
		if err != nil { writeError(w, http.StatusBadGateway, "egress_status_failed", err.Error()); return }
		writeEgressJSON(w, out)
	}))

	mux.Handle("POST /api/egress/nodes/{node}/test", protected(func(w http.ResponseWriter, r *http.Request) {
		node := r.PathValue("node")
		if !validNodeRef(node) { writeError(w, http.StatusBadRequest, "invalid_node", "Некорректная нода"); return }
		out, err := runEgressBridge(r.Context(), "node-test", node)
		if err != nil { writeError(w, http.StatusBadGateway, "egress_node_test_failed", err.Error()); return }
		writeEgressJSON(w, out)
	}))

	mux.Handle("PATCH /api/egress/nodes/{node}", protected(func(w http.ResponseWriter, r *http.Request) {
		node := r.PathValue("node")
		if !validNodeRef(node) { writeError(w, http.StatusBadRequest, "invalid_node", "Некорректная нода"); return }
		var req struct { Name *string `json:"name"`; Enabled *bool `json:"enabled"`; Priority *int `json:"priority"` }
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8<<10)).Decode(&req); err != nil { writeError(w, http.StatusBadRequest, "bad_request", "Некорректное изменение ноды"); return }
		mutations := 0
		if req.Name != nil { mutations++ }; if req.Enabled != nil { mutations++ }; if req.Priority != nil { mutations++ }
		if mutations != 1 { writeError(w, http.StatusBadRequest, "single_mutation_required", "За один запрос изменяется только одно свойство ноды"); return }
		var out []byte; var err error
		switch {
		case req.Name != nil:
			name := strings.TrimSpace(*req.Name)
			if name == "" || len(name) > 64 || strings.ContainsAny(name, "\r\n\x00") { writeError(w, http.StatusBadRequest, "invalid_name", "Имя ноды должно содержать 1–64 символа"); return }
			out, err = runEgressBridge(r.Context(), "node-rename", node, name)
		case req.Enabled != nil:
			if *req.Enabled { out, err = runEgressBridge(r.Context(), "node-enable", node) } else { out, err = runEgressBridge(r.Context(), "node-disable", node) }
		case req.Priority != nil:
			if *req.Priority < 1 || *req.Priority > 9999 { writeError(w, http.StatusBadRequest, "invalid_priority", "Приоритет должен быть от 1 до 9999"); return }
			out, err = runEgressBridge(r.Context(), "node-priority", node, strconv.Itoa(*req.Priority))
		}
		if err != nil { writeError(w, http.StatusBadGateway, "egress_node_update_failed", err.Error()); return }
		writeEgressJSON(w, out)
	}))

	mux.Handle("GET /api/egress/provisioner", protected(func(w http.ResponseWriter, r *http.Request) {
		out, err := runEgressBridge(r.Context(), "provision-preflight")
		if err != nil { writeError(w, http.StatusServiceUnavailable, "egress_provisioner_failed", err.Error()); return }
		writeEgressJSON(w, out)
	}))

	mux.Handle("POST /api/egress/nodes", protected(func(w http.ResponseWriter, r *http.Request) {
		var req struct { Name string `json:"name"`; Host string `json:"host"`; Port int `json:"port"`; User string `json:"user"`; Priority *int `json:"priority"`; Auth egressSSHAuthRequest `json:"auth"` }
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 128<<10)).Decode(&req); err != nil { writeError(w, http.StatusBadRequest, "bad_request", "Некорректные параметры EXIT-ноды"); return }
		req.Name = strings.TrimSpace(req.Name); req.Host = strings.TrimSpace(req.Host); req.User = strings.TrimSpace(req.User)
		if req.Name == "" || len(req.Name) > 64 || strings.ContainsAny(req.Name, "\r\n\x00") { writeError(w, http.StatusBadRequest, "invalid_name", "Имя ноды должно содержать 1–64 символа"); return }
		if req.Host == "" || len(req.Host) > 253 || strings.ContainsAny(req.Host, " \t\r\n\x00") { writeError(w, http.StatusBadRequest, "invalid_host", "Некорректный IP/домен SSH"); return }
		if req.Port == 0 { req.Port = 22 }; if req.Port < 1 || req.Port > 65535 { writeError(w, http.StatusBadRequest, "invalid_port", "SSH port должен быть 1–65535"); return }
		if req.User == "" { req.User = "root" }; if req.User != "root" { writeError(w, http.StatusBadRequest, "invalid_user", "В текущей версии provisioning выполняется через root SSH"); return }
		if req.Priority != nil && (*req.Priority < 1 || *req.Priority > 9999) { writeError(w, http.StatusBadRequest, "invalid_priority", "Priority должен быть от 1 до 9999"); return }
		if err := validateEgressSSHAuth(req.Auth); err != nil { writeError(w, http.StatusBadRequest, "invalid_auth", err.Error()); return }
		payload := map[string]any{"action":"add", "name":req.Name, "host":req.Host, "port":req.Port, "user":req.User, "auth":map[string]any{"mode":req.Auth.Mode,"secret":req.Auth.Secret}}
		if req.Priority != nil { payload["priority"] = *req.Priority }
		raw, _ := json.Marshal(payload); out, err := runEgressBridgeInput(r.Context(), raw, "job-start")
		if err != nil { writeError(w, http.StatusBadGateway, "egress_add_start_failed", err.Error()); return }
		writeEgressJSON(w, out)
	}))

	mux.Handle("POST /api/egress/nodes/{node}/remove", protected(func(w http.ResponseWriter, r *http.Request) {
		node := r.PathValue("node")
		if !validNodeRef(node) { writeError(w, http.StatusBadRequest, "invalid_node", "Некорректная нода"); return }
		var req struct { RemoteCleanup bool `json:"remote_cleanup"`; Fallback string `json:"fallback"`; Auth egressSSHAuthRequest `json:"auth"` }
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 128<<10)).Decode(&req); err != nil { writeError(w, http.StatusBadRequest, "bad_request", "Некорректные параметры удаления"); return }
		req.Fallback = strings.ToLower(strings.TrimSpace(req.Fallback)); if req.Fallback == "" { req.Fallback = "block" }
		if req.Fallback != "block" && req.Fallback != "direct" { writeError(w, http.StatusBadRequest, "invalid_fallback", "Fallback должен быть BLOCK или DIRECT"); return }
		if req.RemoteCleanup { if err := validateEgressSSHAuth(req.Auth); err != nil { writeError(w, http.StatusBadRequest, "invalid_auth", err.Error()); return } } else { req.Auth = egressSSHAuthRequest{Mode:"auto"} }
		payload := map[string]any{"action":"remove", "node":node, "remote_cleanup":req.RemoteCleanup, "fallback":req.Fallback, "auth":map[string]any{"mode":req.Auth.Mode,"secret":req.Auth.Secret}}
		raw, _ := json.Marshal(payload); out, err := runEgressBridgeInput(r.Context(), raw, "job-start")
		if err != nil { writeError(w, http.StatusBadGateway, "egress_remove_start_failed", err.Error()); return }
		writeEgressJSON(w, out)
	}))

	mux.Handle("GET /api/egress/jobs/{job}", protected(func(w http.ResponseWriter, r *http.Request) {
		job := r.PathValue("job")
		if !validJobID(job) { writeError(w, http.StatusBadRequest, "invalid_job", "Некорректный job ID"); return }
		out, err := runEgressBridge(r.Context(), "job-status", job)
		if err != nil { writeError(w, http.StatusNotFound, "egress_job_failed", err.Error()); return }
		writeEgressJSON(w, out)
	}))

	mux.Handle("GET /api/egress/config", protected(func(w http.ResponseWriter, r *http.Request) {
		out, err := runEgressBridge(r.Context(), "config-get")
		if err != nil { writeError(w, http.StatusServiceUnavailable, "egress_config_failed", err.Error()); return }
		writeEgressJSON(w, out)
	}))

	mux.Handle("PUT /api/egress/config", protected(func(w http.ResponseWriter, r *http.Request) {
		var req struct { CheckInterval int `json:"check_interval"`; FailThreshold int `json:"fail_threshold"`; FailbackHold int `json:"failback_hold"`; HandshakeMaxAge int `json:"handshake_max_age"` }
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<10)).Decode(&req); err != nil { writeError(w, http.StatusBadRequest, "bad_request", "Некорректные настройки"); return }
		if req.CheckInterval < 2 || req.CheckInterval > 60 || req.FailThreshold < 1 || req.FailThreshold > 10 || req.FailbackHold < 5 || req.FailbackHold > 600 || req.HandshakeMaxAge < 30 || req.HandshakeMaxAge > 600 {
			writeError(w, http.StatusBadRequest, "invalid_config", "Допустимо: интервал 2–60 с, ошибок 1–10, failback 5–600 с, handshake 30–600 с"); return
		}
		out, err := runEgressBridge(r.Context(), "config-set", strconv.Itoa(req.CheckInterval), strconv.Itoa(req.FailThreshold), strconv.Itoa(req.FailbackHold), strconv.Itoa(req.HandshakeMaxAge))
		if err != nil { writeError(w, http.StatusBadGateway, "egress_config_failed", err.Error()); return }
		writeEgressJSON(w, out)
	}))

	mux.Handle("GET /api/egress/events", protected(func(w http.ResponseWriter, r *http.Request) {
		limit := 30
		if raw := r.URL.Query().Get("limit"); raw != "" {
			n, err := strconv.Atoi(raw); if err != nil || n < 1 || n > 200 { writeError(w, http.StatusBadRequest, "invalid_limit", "limit должен быть от 1 до 200"); return }; limit = n
		}
		out, err := runEgressBridge(r.Context(), "events", strconv.Itoa(limit))
		if err != nil { writeError(w, http.StatusBadGateway, "egress_events_failed", err.Error()); return }
		writeEgressJSON(w, out)
	}))
}
