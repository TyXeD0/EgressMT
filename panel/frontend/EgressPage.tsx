import { useCallback, useEffect, useMemo, useState } from 'react';
import { Header } from '@/components/layout/Header';
import { Card } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ErrorAlert } from '@/components/ErrorAlert';
import {
  egressApi,
  type EgressAddNodeRequest,
  type EgressConfig,
  type EgressJob,
  type EgressMode,
  type EgressNodeStatus,
  type EgressRemoveNodeRequest,
  type EgressTransportUpgradeRequest,
  type EgressSSHAuthMode,
  type EgressStatus,
} from '@/lib/api';

const fieldClass = 'w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-text-primary';

function AuthFields({ mode, secret, onMode, onSecret }: {
  mode: EgressSSHAuthMode;
  secret: string;
  onMode: (v: EgressSSHAuthMode) => void;
  onSecret: (v: string) => void;
}) {
  return <div className="space-y-2">
    <label className="space-y-1 block">
      <span className="text-xs text-text-secondary">SSH authorization</span>
      <select className={fieldClass} value={mode} onChange={(e) => onMode(e.target.value as EgressSSHAuthMode)}>
        <option value="auto">Existing root SSH key</option>
        <option value="password">Password</option>
        <option value="key">Private key</option>
      </select>
    </label>
    {mode === 'password' && <Input type="password" autoComplete="new-password" value={secret} onChange={(e) => onSecret(e.target.value)} placeholder="SSH password" />}
    {mode === 'key' && <textarea className={`${fieldClass} min-h-32 font-mono`} value={secret} onChange={(e) => onSecret(e.target.value)} placeholder="Paste the SSH private key here" />}
    <div className="text-xs text-text-secondary">Credentials are used only for this operation and are not stored in the node registry or job logs.</div>
  </div>;
}

function NodeCard({ node, active, busy, onSwitch, onTest, onToggle, onPriority, onRename, onUpgrade, onRemove }: {
  node: EgressNodeStatus;
  active: boolean;
  busy: boolean;
  onSwitch: () => void;
  onTest: () => void;
  onToggle: () => void;
  onPriority: (p: number) => void;
  onRename: (name: string) => void;
  onUpgrade: () => void;
  onRemove: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [name, setName] = useState(node.name);
  const [priority, setPriority] = useState(String(node.priority));
  useEffect(() => setName(node.name), [node.name]);
  useEffect(() => setPriority(String(node.priority)), [node.priority]);
  const c = node.connectivity || {};
  const a = node.agent || { reachable: false };
  const tr = node.transport as (typeof node.transport & { random_trailers?: boolean; disable_cookies?: boolean });
  return <Card className="p-4 space-y-3">
    <div className="flex items-start justify-between gap-3">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-semibold text-text-primary">{node.name}</span>
          <Badge variant={node.role === 'primary' ? 'default' : 'outline'}>{node.role?.toUpperCase()}</Badge>
          {active && <Badge variant="success">ACTIVE</Badge>}
        </div>
        <div className="text-xs text-text-secondary mt-1">{node.public_ip || '—'} · {node.id} · priority {node.priority}</div>
      </div>
      <Badge variant={!node.enabled ? 'warning' : node.health ? 'success' : 'danger'}>{!node.enabled ? 'DISABLED' : node.health ? 'HEALTHY' : 'DOWN'}</Badge>
    </div>
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
      <div><div className="text-xs text-text-secondary">AWG</div><div>{node.awg?.up ? 'UP' : 'DOWN'}</div><div className="text-xs text-text-secondary">{node.transport?.profile || 'legacy'} · {node.transport?.header_protection ? 'HPK ON' : 'HPK OFF'} · {tr?.random_trailers ? 'RT ON' : 'RT OFF'} · {tr?.disable_cookies ? 'COOKIES OFF' : 'COOKIES ON'}</div></div>
      <div><div className="text-xs text-text-secondary">Tunnel RTT</div><div>{c.tunnel_rtt_ms != null ? `${c.tunnel_rtt_ms} ms` : '—'}</div></div>
      <div><div className="text-xs text-text-secondary">Telegram</div><div>{c.telegram ? `OK · ${c.telegram_target || 'DC'}` : 'FAIL'}</div></div>
      <div><div className="text-xs text-text-secondary">Agent</div><div>{a.reachable ? 'ONLINE' : 'OFFLINE'}</div></div>
    </div>
    <div className="flex flex-wrap gap-2">
      <Button size="sm" variant="outline" disabled={busy || active || !node.enabled || !node.health} onClick={onSwitch}>Make active</Button>
      <Button size="sm" variant="outline" disabled={busy} onClick={onTest}>Test</Button>
      <Button size="sm" variant="outline" onClick={() => setExpanded((v) => !v)}>{expanded ? 'Less' : 'Details'}</Button>
    </div>
    {expanded && <div className="border-t border-border pt-3 space-y-3">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div className="flex gap-2"><Input value={name} maxLength={64} onChange={(e) => setName(e.target.value)} /><Button size="sm" disabled={busy || !name.trim() || name.trim() === node.name} onClick={() => onRename(name.trim())}>Rename</Button></div>
        <div className="flex gap-2"><Input type="number" min={1} max={9999} value={priority} onChange={(e) => setPriority(e.target.value)} /><Button size="sm" disabled={busy || Number(priority) === node.priority} onClick={() => onPriority(Number(priority))}>Priority</Button></div>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant={node.enabled ? 'danger' : 'outline'} disabled={busy} onClick={onToggle}>{node.enabled ? 'Disable from AUTO' : 'Enable node'}</Button>
        <Button size="sm" variant="outline" disabled={busy} onClick={onUpgrade}>Upgrade AWG 3.1…</Button>
        <Button size="sm" variant="danger" disabled={busy} onClick={onRemove}>Remove…</Button>
      </div>
    </div>}
  </Card>;
}

function AddNode({ busy, suggestedPriority, onCancel, onAdd }: {
  busy: boolean;
  suggestedPriority: number;
  onCancel: () => void;
  onAdd: (r: EgressAddNodeRequest) => void;
}) {
  const [name, setName] = useState('');
  const [host, setHost] = useState('');
  const [port, setPort] = useState('22');
  const [priority, setPriority] = useState(String(suggestedPriority));
  const [mode, setMode] = useState<EgressSSHAuthMode>('auto');
  const [secret, setSecret] = useState('');
  const submit = () => {
    const p = Number(port), pr = Number(priority);
    if (!name.trim() || !host.trim() || !Number.isInteger(p) || p < 1 || p > 65535 || !Number.isInteger(pr) || pr < 1 || pr > 9999) return;
    if (mode !== 'auto' && !secret) return;
    onAdd({ name: name.trim(), host: host.trim(), port: p, user: 'root', priority: pr, auth: { mode, secret: mode === 'auto' ? undefined : secret } });
    setSecret('');
  };
  return <Card className="p-4 space-y-4">
    <div><div className="font-medium text-text-primary">Add egress node</div><div className="text-xs text-text-secondary mt-1">Use a clean Ubuntu VPS with root SSH and working access to Telegram servers. EgressMT installs AmneziaWG, firewall/NAT and the node agent automatically.</div></div>
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
      <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Node name, e.g. backup-1" />
      <Input value={host} onChange={(e) => setHost(e.target.value)} placeholder="Egress VPS IP or hostname" />
      <Input type="number" min={1} max={65535} value={port} onChange={(e) => setPort(e.target.value)} placeholder="SSH port" />
      <Input type="number" min={1} max={9999} value={priority} onChange={(e) => setPriority(e.target.value)} placeholder="Priority" />
    </div>
    <AuthFields mode={mode} secret={secret} onMode={(v) => { setMode(v); setSecret(''); }} onSecret={setSecret} />
    <div className="flex gap-2"><Button size="sm" disabled={busy} onClick={submit}>Add node</Button><Button size="sm" variant="outline" disabled={busy} onClick={onCancel}>Cancel</Button></div>
  </Card>;
}

function UpgradeTransport({ node, busy, onCancel, onUpgrade }: {
  node: EgressNodeStatus;
  busy: boolean;
  onCancel: () => void;
  onUpgrade: (r: EgressTransportUpgradeRequest) => void;
}) {
  const [mode, setMode] = useState<EgressSSHAuthMode>('auto');
  const [secret, setSecret] = useState('');
  const submit = () => {
    if (mode !== 'auto' && !secret) return;
    onUpgrade({ auth: { mode, secret: mode === 'auto' ? undefined : secret } });
    setSecret('');
  };
  return <Card className="p-4 space-y-4">
    <div><div className="font-medium text-text-primary">Upgrade transport: {node.name}</div><div className="text-xs text-text-secondary mt-1">Rotates the AWG key pair and UDP port, installs/updates AWG 3.1 on both sides, writes the full obfuscation profile, verifies Telegram through the tunnel, and rolls back both configs if validation fails.</div></div>
    <AuthFields mode={mode} secret={secret} onMode={(v) => { setMode(v); setSecret(''); }} onSecret={setSecret} />
    <div className="flex gap-2"><Button size="sm" disabled={busy} onClick={submit}>Upgrade to AWG 3.1</Button><Button size="sm" variant="outline" disabled={busy} onClick={onCancel}>Cancel</Button></div>
  </Card>;
}

export function EgressPage() {
  const [status, setStatus] = useState<EgressStatus | null>(null);
  const [config, setConfig] = useState<EgressConfig | null>(null);
  const [events, setEvents] = useState<string[]>([]);
  const [job, setJob] = useState<EgressJob | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [upgradeTarget, setUpgradeTarget] = useState<EgressNodeStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const nodes = useMemo(() => [...(status?.nodes || [])].sort((a, b) => a.priority - b.priority || a.name.localeCompare(b.name)), [status]);
  const jobBusy = job?.state === 'queued' || job?.state === 'running';
  const suggestedPriority = Math.min(9999, Math.max(10, (Math.floor(Math.max(0, ...nodes.map((n) => n.priority)) / 10) + 1) * 10));

  const load = useCallback(async () => {
    try {
      const [s, c, e] = await Promise.all([egressApi.status(), egressApi.config(), egressApi.events(30)]);
      setStatus(s); setConfig(c); setEvents(e); setError(null);
    } catch (e) { setError(e instanceof Error ? e.message : 'Failed to load EgressMT status'); }
  }, []);

  useEffect(() => { void load(); const t = window.setInterval(() => void egressApi.status().then(setStatus).catch(() => undefined), 5000); return () => window.clearInterval(t); }, [load]);
  useEffect(() => {
    if (!job || (job.state !== 'queued' && job.state !== 'running')) return;
    const t = window.setInterval(() => void egressApi.job(job.id).then((j) => { setJob(j); if (j.state === 'done' || j.state === 'error') void load(); }).catch((e) => setError(String(e))), 1500);
    return () => window.clearInterval(t);
  }, [job?.id, job?.state, load]);

  const mutate = async (fn: () => Promise<unknown>) => { setBusy(true); try { await fn(); await load(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); } };
  const setMode = async (mode: EgressMode, node?: string) => { if (mode === 'direct' && !window.confirm('DIRECT sends Telegram traffic through the entry VPS. Continue?')) return; if (mode === 'block' && !window.confirm('BLOCK stops Telegram traffic. Continue?')) return; await mutate(() => egressApi.setMode(mode, node)); };
  const startAdd = async (r: EgressAddNodeRequest) => { setBusy(true); try { setJob(await egressApi.addNode(r)); setAddOpen(false); } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); } };
  const startUpgrade = async (node: EgressNodeStatus, r: EgressTransportUpgradeRequest) => { setBusy(true); try { setJob(await egressApi.upgradeTransport(node.id, r)); setUpgradeTarget(null); } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); } };
  const removeNode = async (node: EgressNodeStatus) => {
    if (!window.confirm(`Remove egress node "${node.name}" from the entry VPS?`)) return;
    const remote = window.confirm('Also remove EgressMT components from the remote egress VPS? Cancel = remove only from the entry VPS.');
    const req: EgressRemoveNodeRequest = { remote_cleanup: remote, fallback: 'block', auth: { mode: 'auto' } };
    setBusy(true); try { setJob(await egressApi.removeNode(node.id, req)); } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };

  return <div>
    <Header title="EgressMT — egress nodes" refreshing={busy} onRefresh={load} />
    <div className="p-4 lg:p-6 space-y-4">
      <p className="text-sm text-text-secondary max-w-4xl">Telegram egress for an entry VPS located where Telegram servers are unreachable or unreliable. AUTO uses the healthy egress node with the lowest priority number and fails closed when no egress node is available.</p>
      {error && <ErrorAlert message={error} onRetry={load} />}
      {job && <Card className="p-4"><div className="flex justify-between gap-3"><div><div className="font-medium">{job.label || job.action}</div><div className="text-xs text-text-secondary">{job.message || job.stage || job.state}</div></div><Badge variant={job.state === 'done' ? 'success' : job.state === 'error' ? 'danger' : 'warning'}>{job.state.toUpperCase()}</Badge></div>{job.error && <pre className="text-xs text-danger mt-2 whitespace-pre-wrap">{job.error}</pre>}</Card>}
      {status && <Card className="p-4 space-y-3"><div className="flex flex-wrap justify-between gap-3"><div><div className="font-medium">Mode: {status.mode.toUpperCase()}</div><div className="text-xs text-text-secondary">Active: {nodes.find((n) => n.id === status.active_node)?.name || status.active_node || '—'} · writers {status.telemt?.alive_writers ?? '—'}/{status.telemt?.required_writers ?? '—'} · coverage {status.telemt?.dc_coverage_pct ?? '—'}%</div></div><Badge variant={status.active_node === 'block' ? 'danger' : status.phase === 'running' ? 'success' : 'warning'}>{status.phase || 'unknown'}</Badge></div><div className="flex gap-2"><Button size="sm" variant={status.mode === 'auto' ? 'default' : 'outline'} disabled={busy || jobBusy} onClick={() => void setMode('auto')}>AUTO</Button><Button size="sm" variant="outline" disabled={busy || jobBusy} onClick={() => void setMode('direct')}>DIRECT</Button><Button size="sm" variant="danger" disabled={busy || jobBusy} onClick={() => void setMode('block')}>BLOCK</Button></div></Card>}
      <div className="flex justify-between items-center"><div className="font-medium">Egress nodes: {nodes.length}</div><Button size="sm" variant="outline" disabled={busy || jobBusy} onClick={() => setAddOpen(true)}>+ Add node</Button></div>
      {addOpen && <AddNode busy={busy || Boolean(jobBusy)} suggestedPriority={suggestedPriority} onCancel={() => setAddOpen(false)} onAdd={(r) => void startAdd(r)} />}
      {upgradeTarget && <UpgradeTransport node={upgradeTarget} busy={busy || Boolean(jobBusy)} onCancel={() => setUpgradeTarget(null)} onUpgrade={(r) => void startUpgrade(upgradeTarget, r)} />}
      <div className="grid grid-cols-1 2xl:grid-cols-2 gap-4">{nodes.map((n) => <NodeCard key={n.id} node={n} active={status?.active_node === n.id} busy={busy || Boolean(jobBusy)} onSwitch={() => void setMode('manual', n.id)} onTest={() => void mutate(() => egressApi.testNode(n.id))} onToggle={() => void mutate(() => egressApi.setNodeEnabled(n.id, !n.enabled))} onPriority={(p) => void mutate(() => egressApi.setNodePriority(n.id, p))} onRename={(name) => void mutate(() => egressApi.renameNode(n.id, name))} onUpgrade={() => setUpgradeTarget(n)} onRemove={() => void removeNode(n)} />)}</div>
      {config && <Card className="p-4 space-y-3"><div className="font-medium">Failover settings</div><div className="text-xs text-text-secondary">Check interval: {config.check_interval}s · failures: {config.fail_threshold} · failback hold: {config.failback_hold}s · handshake age: {config.handshake_max_age}s</div></Card>}
      <Card className="p-4"><div className="font-medium mb-2">Events</div><div className="space-y-1 font-mono text-xs text-text-secondary">{[...events].reverse().map((e, i) => <div key={`${i}-${e}`} className="break-all">{e}</div>)}</div></Card>
    </div>
  </div>;
}
