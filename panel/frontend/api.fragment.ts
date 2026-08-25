// ── EgressMT ────────────────────────────────────────────────────────────────

export type EgressMode = 'auto' | 'direct' | 'block' | 'manual';
export type EgressSSHAuthMode = 'auto' | 'password' | 'key';
export type EgressJobState = 'queued' | 'running' | 'done' | 'error';

export interface EgressAgentStatus { reachable: boolean; request_ms?: number | null; version?: string | null; error?: string | null; }
export interface EgressCPUStatus { usage_percent?: number; load1?: number; load5?: number; load15?: number; logical_cpus?: number; }
export interface EgressMemoryStatus { total_bytes?: number; used_bytes?: number; available_bytes?: number; usage_percent?: number; }
export interface EgressDiskStatus { mount?: string; total_bytes?: number; used_bytes?: number; available_bytes?: number; usage_percent?: number; }
export interface EgressNetworkStatus { interface?: string | null; rx_bytes?: number; tx_bytes?: number; rx_bytes_per_sec?: number; tx_bytes_per_sec?: number; rx_bits_per_sec?: number; tx_bits_per_sec?: number; }
export interface EgressSystemStatus { hostname?: string | null; uptime_sec?: number; cpu?: EgressCPUStatus; memory?: EgressMemoryStatus; disk?: EgressDiskStatus; network?: EgressNetworkStatus; }

export interface EgressNodeStatus {
  id: string;
  name: string;
  enabled: boolean;
  priority: number;
  role: string;
  public_ip: string;
  health: boolean;
  fail_count?: number;
  awg: { interface: string; up: boolean; handshake_age_sec: number; rx_bytes?: number; tx_bytes?: number; };
  transport?: { profile?: string; header_protection?: boolean; mtu?: number; updated_at?: string | null; };
  connectivity: { tunnel?: boolean; tunnel_rtt_ms?: number | null; telegram: boolean; telegram_tcp_ms?: number | null; telegram_target?: string | null; telegram_ip?: string | null; telegram_probes_tried?: number; telegram_probe_total?: number; routing?: boolean; nftables?: boolean; };
  agent?: EgressAgentStatus;
  system?: EgressSystemStatus | null;
}

export interface EgressTelemtStatus { nat_ip?: string | null; dc_available?: boolean | null; dc_verdict?: string | null; dc_coverage_pct?: number | null; alive_writers?: number | null; required_writers?: number | null; }
export interface EgressStatus { version?: string; timestamp?: string; phase?: string; mode: EgressMode; manual_node?: string | null; active_node: string; last_error?: string | null; telemt?: EgressTelemtStatus; nodes: EgressNodeStatus[]; }
export interface EgressConfig { check_interval: number; fail_threshold: number; failback_hold: number; handshake_max_age: number; }
export interface EgressSSHAuth { mode: EgressSSHAuthMode; secret?: string; }
export interface EgressAddNodeRequest { name: string; host: string; port: number; user: string; priority?: number; auth: EgressSSHAuth; }
export interface EgressRemoveNodeRequest { remote_cleanup: boolean; fallback: 'block' | 'direct'; auth: EgressSSHAuth; }
export interface EgressTransportUpgradeRequest { auth: EgressSSHAuth; }
export interface EgressProvisionerStatus { ok: boolean; version: string; nodes: number; active?: string | null; }
export interface EgressJob { id: string; action: 'add' | 'remove' | 'upgrade'; label?: string; state: EgressJobState; stage?: string; message?: string; created_at?: number; updated_at?: number; finished_at?: number | null; result?: Record<string, unknown> | null; error?: string | null; }

const EGRESS_BASE = `${BASE}/api/egress`;

export const egressApi = {
  status: () => request<EgressStatus>(EGRESS_BASE, '/status'),
  setMode: (mode: EgressMode, node?: string) => request<EgressStatus>(EGRESS_BASE, '/mode', { method: 'POST', body: JSON.stringify({ mode, node }) }),
  testNode: (node: string) => request<EgressNodeStatus>(EGRESS_BASE, `/nodes/${encodeURIComponent(node)}/test`, { method: 'POST' }),
  renameNode: (node: string, name: string) => request<Record<string, unknown>>(EGRESS_BASE, `/nodes/${encodeURIComponent(node)}`, { method: 'PATCH', body: JSON.stringify({ name }) }),
  setNodeEnabled: (node: string, enabled: boolean) => request<Record<string, unknown>>(EGRESS_BASE, `/nodes/${encodeURIComponent(node)}`, { method: 'PATCH', body: JSON.stringify({ enabled }) }),
  setNodePriority: (node: string, priority: number) => request<Record<string, unknown>>(EGRESS_BASE, `/nodes/${encodeURIComponent(node)}`, { method: 'PATCH', body: JSON.stringify({ priority }) }),
  provisioner: () => request<EgressProvisionerStatus>(EGRESS_BASE, '/provisioner'),
  addNode: (payload: EgressAddNodeRequest) => request<EgressJob>(EGRESS_BASE, '/nodes', { method: 'POST', body: JSON.stringify(payload) }),
  removeNode: (node: string, payload: EgressRemoveNodeRequest) => request<EgressJob>(EGRESS_BASE, `/nodes/${encodeURIComponent(node)}/remove`, { method: 'POST', body: JSON.stringify(payload) }),
  upgradeTransport: (node: string, payload: EgressTransportUpgradeRequest) => request<EgressJob>(EGRESS_BASE, `/nodes/${encodeURIComponent(node)}/transport`, { method: 'POST', body: JSON.stringify(payload) }),
  job: (job: string) => request<EgressJob>(EGRESS_BASE, `/jobs/${encodeURIComponent(job)}`),
  config: () => request<EgressConfig>(EGRESS_BASE, '/config'),
  saveConfig: (config: EgressConfig) => request<EgressConfig>(EGRESS_BASE, '/config', { method: 'PUT', body: JSON.stringify(config) }),
  events: (limit = 30) => request<string[]>(EGRESS_BASE, `/events?limit=${limit}`),
};
