#!/usr/bin/env python3
from pathlib import Path
import shutil
import sys

if len(sys.argv) != 3:
    raise SystemExit("usage: patch.py PANEL_SRC ASSET_DIR")

panel = Path(sys.argv[1]).resolve()
assets = Path(sys.argv[2]).resolve()

shutil.copy2(assets / "backend" / "egress.go", panel / "internal" / "server" / "egress.go")
shutil.copy2(assets / "frontend" / "EgressPage.tsx", panel / "frontend" / "src" / "pages" / "EgressPage.tsx")

api = panel / "frontend" / "src" / "lib" / "api.ts"
s = api.read_text()
marker = "// ── EgressMT ────────────────────────────────────────────────────────────────"
if marker not in s:
    s = s.rstrip() + "\n\n" + (assets / "frontend" / "api.fragment.ts").read_text().lstrip()
    api.write_text(s)

app = panel / "frontend" / "src" / "App.tsx"
s = app.read_text()
if "EgressPage" not in s:
    anchor = "import { TgbotPage } from '@/pages/TgbotPage';\n"
    if anchor not in s:
        raise SystemExit("App.tsx: import anchor not found")
    s = s.replace(anchor, anchor + "import { EgressPage } from '@/pages/EgressPage';\n", 1)
if 'path="/egress"' not in s:
    anchor = '          <Route path="/tgbot" element={<TgbotPage />} />\n'
    if anchor not in s:
        raise SystemExit("App.tsx: route anchor not found")
    s = s.replace(anchor, '          <Route path="/egress" element={<EgressPage />} />\n' + anchor, 1)
app.write_text(s)

sidebar = panel / "frontend" / "src" / "components" / "layout" / "Sidebar.tsx"
s = sidebar.read_text()
if "'/egress'" not in s:
    anchor = "  { to: '/warp', icon: Waypoints, label: 'Telegram через WARP', managerOnly: false },\n"
    if anchor not in s:
        raise SystemExit("Sidebar.tsx: nav anchor not found")
    s = s.replace(anchor, anchor + "  { to: '/egress', icon: Network, label: 'EgressMT · EXIT nodes', managerOnly: false },\n", 1)
sidebar.write_text(s)

server = panel / "internal" / "server" / "server.go"
s = server.read_text()
if "registerEgressRoutes" not in s:
    anchor = "\t// MTProxyL host-level endpoints (mode, selfmask, backups)\n\ts.registerMtproxylRoutes(mux, jwtSecret)\n"
    if anchor not in s:
        raise SystemExit("server.go: route registration anchor not found")
    s = s.replace(anchor, anchor + "\n\t// EgressMT API.\n\ts.registerEgressRoutes(mux, jwtSecret)\n", 1)

if "customPanelBuild :=" not in s:
    anchor = "\t// Panel update endpoints\n\tpanelUpd := panel_updater.New(\n"
    if anchor not in s:
        raise SystemExit("server.go: updater anchor not found")
    s = s.replace(
        anchor,
        "\t// Panel update endpoints\n"
        "\t// EgressMT builds must not overwrite themselves with an upstream binary.\n"
        "\tcustomPanelBuild := strings.Contains(version, \"-egressmt\")\n"
        "\tpanelUpd := panel_updater.New(\n",
        1,
    )

apply_anchor = '\tmux.Handle("POST /api/panel/update/apply", auth.RequireAuth(jwtSecret, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {\n'
if '"custom_build"' not in s:
    if apply_anchor not in s:
        raise SystemExit("server.go: panel update apply anchor not found")
    s = s.replace(
        apply_anchor,
        apply_anchor
        + '\t\tif customPanelBuild {\n'
        + '\t\t\twriteError(w, http.StatusConflict, "custom_build", "EgressMT panel integration must be updated through the EgressMT installer.")\n'
        + '\t\t\treturn\n'
        + '\t\t}\n',
        1,
    )

old = '\t\tApplyFn: func(version string) error { return panelUpd.Apply(version) },\n'
new = (
    '\t\tApplyFn: func(version string) error {\n'
    '\t\t\tif customPanelBuild {\n'
    '\t\t\t\treturn fmt.Errorf("EgressMT panel build cannot be overwritten by the upstream updater")\n'
    '\t\t\t}\n'
    '\t\t\treturn panelUpd.Apply(version)\n'
    '\t\t},\n'
)
if old in s:
    s = s.replace(old, new, 1)
elif "EgressMT panel build cannot be overwritten" not in s:
    raise SystemExit("server.go: panel auto-update ApplyFn anchor not found")
server.write_text(s)

print("EgressMT panel source patched successfully")
