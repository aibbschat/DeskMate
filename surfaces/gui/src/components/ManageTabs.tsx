import { useEffect, useState } from "react";
import {
  addMcpServer,
  allowUser,
  connectConnector,
  connectManaged,
  connectMcpBacked,
  connectMcp,
  deleteMcpServer,
  disallowUser,
  getMcpServers,
  getMcpTools,
  signoutMcp,
  validatePlugin,
  installPlugin,
  getSettings,
  getSubscriptions,
  removeModel,
  resolveUnauthorized,
  unsubscribeChannel,
  patchMcpServer,
  reloadMcp,
  setDefaultModel,
  updateConnectorTools,
  type CloudStatus,
  type Connector,
  type Subscription,
  type McpServer,
  type ModelSettings,
  type ProviderInfo,
  type PluginValidateResult,
  type PluginInstallResult,
} from "../api";
import { CloudSignInInline, CloudStatusPending } from "./connectors/CloudSignIn";
import { ModelChecklist } from "./ModelChecklist";
import { ProviderCards, ProviderForm, useProviderSetup } from "../providers/ProviderSetup";
import { Toggle } from "./Toggle";
import { usePolling } from "../hooks/usePolling";

// "2h ago"-style label for the providers' Last-used line (null when never used).
const relTime = (epoch?: number | null): string | null => {
  if (!epoch) return null;
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (secs < 90) return "just now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 48) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
};

// Shared tab bodies for the Settings and Integrations pages (the old top-tab ManageModal was retired
// when Settings/Activity became full-page surfaces): ModelsTab → Settings ▸ Models; ConnectorsTab +
// McpTab → Integrations ▸ Connectors / MCP servers.
const SEC_H = "text-label uppercase tracking-[0.05em] text-faint font-semibold";
const CARD = "rounded-xl2 border border-line bg-panel";
const INPUT =
  "w-full px-3 py-2 rounded-lg border border-line bg-paper text-ui text-ink outline-none focus:border-accent";
const BTN_BORDERED =
  "text-[12.5px] px-3 py-1.5 rounded-lg border border-line bg-paper hover:border-lineStrong shrink-0";
const BTN_ACCENT = "text-[12.5px] px-3 py-1.5 rounded-lg bg-accent text-white shrink-0 disabled:opacity-50";
const BTN_DANGER = "text-[12.5px] text-danger/80 hover:text-danger shrink-0";

/** Two-letter initials for a chip/avatar (first+last word, else first two chars). */
function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

// -- Configure Models tab (UX-021: the shared provider gallery + key form) ----
// Settings ▸ Models reuses onboarding §39's ProviderCards/ProviderForm so the two
// surfaces can't drift. Settings-only extras: per-card "used Nh ago", a "Remove
// key…" affordance, the global composer-picker card (gallery view), and the
// per-provider ModelChecklist / read-only model preview (form view).
export function ModelsTab() {
  const [settings, setSettings] = useState<ModelSettings | null>(null);
  const refreshSettings = () => getSettings().then(setSettings).catch(() => setSettings(null));
  const ps = useProviderSetup({ onSaved: refreshSettings });
  useEffect(() => {
    refreshSettings();
  }, []);

  if (!settings) return <div className="text-ui text-muted">Loading…</div>;

  const info = ps.info;
  const knownNames = ps.providers.map((p) => p.name);

  if (ps.sel === null) {
    // 每个厂商在 composer 选择器里已有几个模型（"openai:gpt-5" → openai）。归到名字命中
    // 的第一个厂商；裸模型 id（openai 的旧格式）算给 openai。
    const names = ps.providers.map((p) => p.name);
    const provOf = (id: string) => {
      const i = id.indexOf(":");
      return i > 0 && names.includes(id.slice(0, i)) ? id.slice(0, i) : "openai";
    };
    const countFor = (name: string) => settings.models.filter((id) => provOf(id) === name).length;
    return (
      <div>
        <ProviderCards
          ps={ps}
          tp="set"
          gridClass="grid grid-cols-2 xl:grid-cols-3 gap-2.5"
          lastUsed
          refine
          countFor={countFor}
        />
        <ComposerPickerCard settings={settings} providers={ps.providers} onChanged={refreshSettings} />
      </div>
    );
  }

  return (
    <div>
      <ProviderForm
        ps={ps}
        tp="set"
        footer={
          ps.credentialed ? (
            <button
              className="text-[12.5px] text-danger/80 hover:text-danger hover:underline underline-offset-2"
              data-testid="set-remove-key"
              onClick={() => {
                if (window.confirm(`Remove the ${info?.title} key from this computer?`)) ps.removeKey();
              }}
            >
              Remove key…
            </button>
          ) : null
        }
      />

      {ps.sel === "openai" && settings.source === "env" && (
        <p className="text-meta text-muted mt-3 leading-relaxed">
          A key is set via <code>OPENAI_API_KEY</code> in this server's environment. You can override
          it above; the stored key is used only when the environment variable is absent.
        </p>
      )}

      {info?.configured ? (
        <div className="mt-6">
          <div className={SEC_H + " mb-1.5"}>Models</div>
          <p className="text-meta text-muted mb-2.5 leading-relaxed">
            Ticked models show in the composer's picker; the black badge marks the default for new
            sessions.
          </p>
          <ModelChecklist
            provider={ps.sel}
            knownProviders={knownNames}
            suggested={info?.suggested_models || []}
            curated={settings.models}
            defaultModel={settings.model}
            labels={settings.model_labels}
            onChanged={(next) => setSettings((s) => (s ? { ...s, models: next.models, model: next.model } : s))}
          />
        </div>
      ) : (
        // Unconfigured providers still show their curated models as a read-only preview — what a
        // key unlocks is part of deciding to get one at all (owner ask, 2026-07-04).
        (info?.suggested_models?.length || 0) > 0 && (
          <div className="mt-6" data-testid="model-preview">
            <div className={SEC_H + " mb-1.5"}>Included models</div>
            <p className="text-meta text-muted mb-2.5 leading-relaxed">
              Curated, agent-capable models this provider serves — add your key above to enable them.
            </p>
            <div className="space-y-1">
              {(info?.suggested_models || []).map((m) => {
                const full = ps.sel === "openai" ? m : `${ps.sel}:${m}`;
                return (
                  <div
                    key={m}
                    className="px-2.5 py-1.5 rounded-lg border border-line bg-paper text-ui text-muted"
                    title={full}
                  >
                    {settings.model_labels?.[full] || m}
                  </div>
                );
              })}
            </div>
          </div>
        )
      )}
    </div>
  );
}

// The gallery view's "In the composer's picker" card: every curated model across providers,
// with its provider tag. Unticking removes it from the picker; adding happens from a
// provider's card (the ModelChecklist there has the suggested list + free-type add).
function ComposerPickerCard({
  settings,
  providers,
  onChanged,
}: {
  settings: ModelSettings;
  providers: ProviderInfo[];
  onChanged: () => void;
}) {
  const [q, setQ] = useState("");
  const names = providers.map((p) => p.name);
  const provOf = (id: string) => {
    const i = id.indexOf(":");
    return i > 0 && names.includes(id.slice(0, i)) ? id.slice(0, i) : "openai";
  };
  const tag = (id: string) => {
    const p = providers.find((x) => x.name === provOf(id));
    return (p?.title || provOf(id)).split(" (")[0];
  };
  return (
    <div className="mt-6" data-testid="composer-picker">
      <div className={SEC_H + " mb-1.5"}>In the composer's picker</div>
      <p className="text-meta text-muted mb-2.5 leading-relaxed">
        The models offered when starting a session; the black badge marks the default. Add more
        from a provider's card above.
      </p>
      {settings.models.length > 8 && (
        <input
          className="w-full mb-2 px-3 py-1.5 rounded-lg border border-line bg-panel text-ui outline-none focus:border-accent"
          placeholder="Filter models…"
          value={q}
          data-testid="composer-picker-filter"
          onChange={(e) => setQ(e.target.value)}
        />
      )}
      <div className="mlist">
        {settings.models
          .filter((id) =>
            q.trim()
              ? `${settings.model_labels?.[id] || ""} ${id} ${tag(id)}`
                  .toLowerCase()
                  .includes(q.trim().toLowerCase())
              : true,
          )
          .map((id) => {
          const isDefault = id === settings.model;
          return (
            <div className="mlist-row" key={id}>
              <label className="mlist-main">
                <input
                  type="checkbox"
                  checked
                  disabled={isDefault}
                  title={isDefault ? "The default model is always shown — make another model default first" : "Remove from the picker"}
                  onChange={() => removeModel(id).then((r) => r.ok && onChanged())}
                />
                <span className="mlist-name" title={id}>
                  {settings.model_labels?.[id] || id}
                </span>
              </label>
              <span className="text-label text-faint mr-2 shrink-0">{tag(id)}</span>
              {isDefault ? (
                <span className="mlist-default">default</span>
              ) : (
                <button className="mlist-make" onClick={() => setDefaultModel(id).then(() => onChanged())}>
                  Make default
                </button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// Curated marketplace presets — high-usage, office-productivity MCP servers.
// Domestic big-vendor (阿里百炼 / 腾讯元器) plus widely-used independents (Fetch /
// Brave / Notion / Granola). Anything Claude/Cursor/Codex-compatible drops in via the
// Add form above; these are the ones we pre-approve as "just works".
//
// `auth` drives the preset card UI:
//   - "oauth" → one-click Connect opens browser sign-in (tokens in SecretStore)
//   - "key"   → inline key field; for HTTP servers `header` carries the Authorization
//               template (with `__KEY__`), for stdio servers `envKey` is injected into
//               the process env. The key is written into the stored config at connect
//               time (no `${VAR}` resolution needed).
type PresetEntry = {
  name: string;
  label: string;
  blurb: string;
  auth?: "oauth" | "key";
  envKey?: string;
  header?: { name: string; value: string };
  keyHint?: string;
  config: Record<string, any>;
};

const MCP_PRESETS: PresetEntry[] = [
  {
    name: "granola",
    label: "Granola",
    blurb: "会议记录与转写 · 用 Granola 账号登录（OAuth，无需密钥）",
    auth: "oauth",
    config: { type: "http", url: "https://mcp.granola.ai/mcp", auth: "oauth" },
  },
  {
    name: "bailian-websearch",
    label: "阿里百炼 · 联网搜索",
    blurb: "百炼 MCP 广场 · 一个 Key 调全平台服务（搜索 / 地图 / 法律 / 金融…）",
    auth: "key",
    envKey: "DASHSCOPE_API_KEY",
    keyHint: "阿里云百炼 API Key（dashscope.console.aliyun.com → API Key，格式 sk-xxx）",
    header: { name: "Authorization", value: "Bearer __KEY__" },
    config: { type: "http", url: "https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp" },
  },
  {
    name: "tencent-sse",
    label: "腾讯云 · 托管 MCP (SSE)",
    blurb: "联网搜索 / 元器智能体等。腾讯云 MCP 广场开通服务 → 复制「专属 SSE URL」粘贴下方 → 连接",
    config: { type: "sse", url: "" },
  },
  {
    name: "fetch",
    label: "Fetch · 网页抓取",
    blurb: "抓取任意 URL 转为 Markdown · 无需密钥 · 读网页 / 文档",
    config: { type: "stdio", command: "uvx", args: ["mcp-server-fetch"] },
  },
  {
    name: "brave-search",
    label: "Brave 搜索",
    blurb: "联网搜索 · 免费额度友好",
    auth: "key",
    envKey: "BRAVE_API_KEY",
    keyHint: "Brave Search API Key（brave.com/search/api）",
    config: { type: "stdio", command: "npx", args: ["-y", "@modelcontextprotocol/server-brave-search"] },
  },
  {
    name: "notion",
    label: "Notion",
    blurb: "Notion 页面 / 数据库读写 · 团队知识库",
    auth: "key",
    envKey: "NOTION_TOKEN",
    keyHint: "Notion integration token（notion.so/my-integrations，格式 secret_xxx）",
    config: { type: "stdio", command: "npx", args: ["-y", "@notionhq/notion-mcp-server"] },
  },
];

// Plugin package installer: validate a local path or inline JSON, then install its
// MCP servers + persona snapshots in one shot (the backend is the source of truth).
function PluginInstallPanel({ onInstalled }: { onInstalled: () => void }) {
  const [path, setPath] = useState("");
  const [payload, setPayload] = useState("");
  const [showJson, setShowJson] = useState(false);
  const [validating, setValidating] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [result, setResult] = useState<PluginValidateResult | PluginInstallResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const canValidate = path.trim().length > 0 || payload.trim().length > 0;

  const buildBody = (): { path?: string; payload?: string } => {
    if (path.trim()) return { path: path.trim() };
    return { payload };
  };

  const runValidate = async () => {
    setError(null);
    setValidating(true);
    setResult(null);
    try {
      const r = await validatePlugin(buildBody());
      setResult(r);
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setValidating(false);
    }
  };

  const runInstall = async () => {
    setError(null);
    setInstalling(true);
    try {
      const r = await installPlugin(buildBody());
      setResult(r);
      if (r.ok) {
        onInstalled();
        setPath("");
        setPayload("");
      } else {
        setError(r.error || (r.errors && r.errors.join("; ")) || "安装失败");
      }
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setInstalling(false);
    }
  };

  const ok = !!result && result.ok;

  return (
    <div className={CARD + " p-3.5 space-y-2.5"}>
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-body font-medium">插件包安装</span>
        <span className="text-[10.5px] normal-case tracking-normal text-faint">
          · .json / .zip 本地包，一键装 server + 角色
        </span>
      </div>
      <input
        className={INPUT}
        placeholder="插件包本地路径，例如 /Users/you/plugins/my-pack.json"
        value={path}
        onChange={(e) => setPath(e.target.value)}
        data-testid="plugin-path"
      />
      <button className={BTN_BORDERED} onClick={() => setShowJson((v) => !v)}>
        {showJson ? "收起 JSON" : "粘贴 JSON 代替路径"}
      </button>
      {showJson && (
        <textarea
          className={INPUT + " h-28 font-mono"}
          placeholder='{ "name": "...", "servers": [...], "personas": [...] }'
          value={payload}
          onChange={(e) => setPayload(e.target.value)}
          data-testid="plugin-payload"
        />
      )}
      <div className="flex items-center gap-2">
        <button
          className={BTN_ACCENT}
          disabled={!canValidate || validating}
          onClick={runValidate}
          data-testid="plugin-validate"
        >
          {validating ? "校验中…" : "校验"}
        </button>
        {ok && (
          <button
            className={BTN_ACCENT}
            disabled={installing}
            onClick={runInstall}
            data-testid="plugin-install"
          >
            {installing ? "安装中…" : "安装"}
          </button>
        )}
      </div>
      {result && (
        <div className="text-[11.5px] leading-relaxed space-y-1">
          {ok ? (
            <div className="text-emerald-600 dark:text-emerald-400">
              ✅ {result.name || "插件包"}：{(result.servers || []).length} 个 server ·{" "}
              {(result.personas || []).length} 个角色已就绪
            </div>
          ) : (
            <div className="text-danger">⚠️ 校验未通过</div>
          )}
          {(result.servers || []).length > 0 && (
            <div className="text-faint">servers: {(result.servers || []).join(", ")}</div>
          )}
          {(result.personas || []).length > 0 && (
            <div className="text-faint">personas: {(result.personas || []).join(", ")}</div>
          )}
          {(result.errors || []).map((e, i) => (
            <div key={i} className="text-danger/80">
              · {e}
            </div>
          ))}
          {result.error && <div className="text-danger/80">· {result.error}</div>}
        </div>
      )}
      {error && <div className="text-[12.5px] text-danger">{error}</div>}
    </div>
  );
}

export function McpTab() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => getMcpServers().then(setServers).catch(() => setServers([]));
  useEffect(() => {
    refresh();
  }, []);

  // While a browser sign-in is in flight, poll so the row flips to connected (or
  // surfaces the error) without the user having to touch anything.
  const authorizing = servers.some((s) => s.status === "authorizing");
  // PR20(#51)：登录进行中的 2s 轮询迁 usePolling（enabled = 有 authorizing 的服务器）。
  usePolling(refresh, 2000, { enabled: authorizing });

  const toggle = async (s: McpServer) => {
    await patchMcpServer(s.name, { enabled: !s.enabled });
    refresh();
  };
  const remove = async (s: McpServer) => {
    await deleteMcpServer(s.name);
    refresh();
  };

  return (
    <div className="space-y-3">
      <p className="text-[12.5px] text-muted leading-relaxed">
        External tool servers (stdio or HTTP), shared across all agents. Enabled servers' tools are
        permission-gated. Changes apply to new sessions —{" "}
        <button
          className="text-accent font-medium hover:underline"
          onClick={() => reloadMcp().then(refresh)}
        >
          reload now
        </button>
        .
      </p>

      {servers.length === 0 && !adding ? (
        <div className={CARD + " p-4 text-ui text-muted"}>
          No MCP servers configured.{" "}
          <button className="text-accent font-medium" onClick={() => setAdding(true)}>
            Add a server
          </button>
        </div>
      ) : (
        <div className="space-y-2">
          {servers.map((s) => (
            <McpRow
              key={s.name}
              server={s}
              onToggle={() => toggle(s)}
              onRemove={() => remove(s)}
              onRefresh={refresh}
            />
          ))}
        </div>
      )}

      {/* Curated marketplace presets (one-click OAuth). This is the thin edge of
          the plugin-center: any Claude/Cursor/Codex-compatible MCP server drops in
          via the Add form above — big-vendor plugins (通义/腾讯/字节/百度) and the
          wider MCP.so / community catalog all "just work". */}
      {MCP_PRESETS.filter((p) => !servers.some((s) => s.name === p.name)).length > 0 && (
        <div className="space-y-2">
          <div className={SEC_H + " flex items-center gap-2"}>
            <span>From the marketplace</span>
            <span className="text-[10.5px] normal-case tracking-normal text-faint">
              · or paste any server below
            </span>
          </div>
          {MCP_PRESETS.filter((p) => !servers.some((s) => s.name === p.name)).map((p) => (
            <PresetCard key={p.name} preset={p} onAdded={refresh} />
          ))}
        </div>
      )}

      <PluginInstallPanel onInstalled={refresh} />

      {adding ? (
        <AddForm
          onCancel={() => {
            setAdding(false);
            setError(null);
          }}
          onError={setError}
          onAdded={() => {
            setAdding(false);
            setError(null);
            refresh();
          }}
        />
      ) : servers.length > 0 ? (
        <button className={BTN_ACCENT} onClick={() => setAdding(true)}>
          + Add server
        </button>
      ) : null}
      {error && <div className="text-[12.5px] text-danger">{error}</div>}
    </div>
  );
}

function McpRow({
  server,
  onToggle,
  onRemove,
  onRefresh,
}: {
  server: McpServer;
  onToggle: () => void;
  onRemove: () => void;
  onRefresh: () => void;
}) {
  const [tools, setTools] = useState<{ name: string; description: string }[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [toolErr, setToolErr] = useState<string | null>(null);

  const isOauth = server.auth === "oauth";
  const authorizing = server.status === "authorizing";
  const signIn = async () => {
    await connectMcp(server.name); // browser opens; the tab's poll flips the status
    onRefresh();
  };
  const signOut = async () => {
    await signoutMcp(server.name);
    onRefresh();
  };

  const loadTools = async () => {
    if (tools) {
      setTools(null);
      return;
    }
    setBusy(true);
    setToolErr(null);
    const res = await getMcpTools(server.name);
    setBusy(false);
    if (res.ok) setTools(res.tools);
    else setToolErr(res.error || "failed to connect");
  };

  return (
    <div className={CARD + " p-3.5"}>
      <div className="flex items-center gap-3">
        <Toggle checked={server.enabled} onChange={onToggle} title="Enable this server" />
        <div className="flex-1 min-w-0">
          <div className="text-body font-medium">{server.name}</div>
          <div className="text-[11.5px] text-faint">
            {server.transport} · {authorizing ? "signing in…" : server.status.replace("_", " ")}
            {server.tool_count != null ? ` · ${server.tool_count} tools` : ""}
            {server.requires_approval ? " · asks" : ""}
            {isOauth ? " · oauth" : ""}
          </div>
        </div>
        {isOauth &&
          (server.status === "needs_auth" ? (
            <button className={BTN_ACCENT} onClick={signIn} data-testid={`mcp-signin-${server.name}`}>
              Sign in
            </button>
          ) : authorizing ? (
            <span className="text-meta text-muted shrink-0">waiting for browser…</span>
          ) : server.status === "connected" ? (
            <button
              className="text-meta text-muted hover:text-ink shrink-0"
              onClick={signOut}
              data-testid={`mcp-signout-${server.name}`}
            >
              sign out
            </button>
          ) : null)}
        <button
          className="text-meta text-muted hover:text-ink shrink-0"
          onClick={loadTools}
          disabled={busy}
        >
          {busy ? "…" : tools ? "hide tools" : "tools"}
        </button>
        <button className={BTN_DANGER} onClick={onRemove}>
          remove
        </button>
      </div>
      {server.last_error && server.status !== "connected" && (
        <div className="text-[12.5px] text-danger mt-1.5">{server.last_error}</div>
      )}
      {toolErr && <div className="text-[12.5px] text-danger mt-1.5">{toolErr}</div>}
      {tools && (
        <div className="mt-2.5 pt-2.5 border-t border-line flex flex-wrap gap-1.5">
          {tools.length === 0 && <div className="text-meta text-faint">No tools.</div>}
          {tools.map((t) => (
            <span
              key={t.name}
              title={t.description}
              className="font-mono text-[11.5px] px-1.5 py-0.5 rounded-md bg-paper border border-line"
            >
              {t.name}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function PresetCard({ preset, onAdded }: { preset: PresetEntry; onAdded: () => void }) {
  const [key, setKey] = useState("");
  const [url, setUrl] = useState(preset.config.url || "");
  const [busy, setBusy] = useState(false);
  const [keyErr, setKeyErr] = useState<string | null>(null);
  const needsKey = preset.auth === "key";
  const isSse = preset.config.type === "sse";

  const connect = async () => {
    if (needsKey && !key.trim()) {
      setKeyErr("请先填写密钥");
      return;
    }
    if (isSse && !url.trim()) {
      setKeyErr("请粘贴专属 SSE URL");
      return;
    }
    setBusy(true);
    setKeyErr(null);
    try {
      const config: Record<string, any> = { ...preset.config };
      if (isSse) config.url = url.trim();
      if (needsKey && key.trim()) {
        const k = key.trim();
        if (preset.header) {
          config.headers = { ...(config.headers || {}), [preset.header.name]: preset.header.value.replace(/__KEY__/g, k) };
        } else if (preset.envKey) {
          config.env = { ...(config.env || {}), [preset.envKey]: k };
        }
      }
      await addMcpServer(preset.name, config);
      await connectMcp(preset.name);
      onAdded();
    } catch (e) {
      setKeyErr(e instanceof Error ? e.message : "连接失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={CARD + " p-3.5 flex flex-col gap-2"} data-testid={`mcp-preset-${preset.name}`}>
      <div className="flex items-center gap-3">
        <div className="flex-1 min-w-0">
          <div className="text-body font-medium">{preset.label}</div>
          <div className="text-[11.5px] text-faint">{preset.blurb}</div>
        </div>
        <button className={BTN_ACCENT} onClick={connect} disabled={busy}>
          {busy ? "连接中…" : "添加并连接"}
        </button>
      </div>
      {isSse && (
        <div className="flex flex-col gap-1">
          <input
            className={INPUT}
            placeholder="粘贴专属 SSE URL（腾讯云 MCP 广场开通服务后复制）"
            value={url}
            onChange={(e) => {
              setUrl(e.target.value);
              if (keyErr) setKeyErr(null);
            }}
          />
        </div>
      )}
      {needsKey && (
        <div className="flex flex-col gap-1">
          <input
            type="password"
            className={INPUT}
            placeholder={preset.keyHint || `输入 ${preset.envKey ?? "密钥"}`}
            value={key}
            onChange={(e) => {
              setKey(e.target.value);
              if (keyErr) setKeyErr(null);
            }}
          />
          {preset.keyHint && !keyErr && (
            <div className="text-[10.5px] text-faint">{preset.keyHint}</div>
          )}
          {keyErr && <div className="text-[10.5px] text-danger">{keyErr}</div>}
        </div>
      )}
    </div>
  );
}

function AddForm({
  onCancel,
  onAdded,
  onError,
}: {
  onCancel: () => void;
  onAdded: () => void;
  onError: (e: string | null) => void;
}) {
  // Structured one-click connect form (replaces the old "paste JSON" box): three
  // shapes — a local stdio command (npx -y @…/server, args, env), a remote
  // streamable HTTP URL (with OAuth optional), or a remote SSE URL (Tencent Cloud
  // hosted MCP, etc.). After adding, we auto-connect and probe the tools so a
  // broken command/url surfaces immediately, not on next session.
  const [kind, setKind] = useState<"stdio" | "http" | "sse">("stdio");
  const [name, setName] = useState("");
  const [command, setCommand] = useState("npx");
  const [argsText, setArgsText] = useState("");
  const [envText, setEnvText] = useState("");
  const [url, setUrl] = useState("");
  const [oauth, setOauth] = useState(false);
  const [busy, setBusy] = useState(false);
  const [probed, setProbed] = useState<number | null>(null);

  const save = async () => {
    onError(null);
    const nm = name.trim();
    if (!nm) {
      onError("Give the server a name.");
      return;
    }
    let config: Record<string, any>;
    if (kind === "stdio") {
      const args = argsText
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean)
        .map((line) => line.split(/\s+/).filter(Boolean))
        .flat();
      const env: Record<string, string> = {};
      for (const ln of envText.split("\n")) {
        const m = ln.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
        if (m) env[m[1]] = m[2];
      }
      config = { command: command.trim() || "npx", args, enabled: true };
      if (Object.keys(env).length) config.env = env;
    } else if (kind === "sse") {
      if (!url.trim()) {
        onError("Enter the SSE URL.");
        return;
      }
      config = { type: "sse", url: url.trim(), enabled: true };
      if (oauth) config.auth = "oauth";
    } else {
      if (!url.trim()) {
        onError("Enter the server URL.");
        return;
      }
      config = { type: "http", url: url.trim(), enabled: true };
      if (oauth) config.auth = "oauth";
    }
    setBusy(true);
    try {
      const add = await addMcpServer(nm, config);
      if (!add.ok) {
        onError(add.error || "could not add");
        return;
      }
      // Auto-connect + probe so the user sees tools (or the error) right away.
      await connectMcp(nm);
      const probe = await getMcpTools(nm);
      setProbed(probe.ok ? probe.tools.length : 0);
      if (!probe.ok) {
        // Added but could not connect — keep it, but tell them why.
        onError(probe.error || "added, but connection failed (fix or reconnect)");
        setBusy(false);
        return;
      }
      onAdded();
    } catch (e: any) {
      onError(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  const KIND = (
    <div className="inline-flex rounded-full p-0.5 bg-paper text-[12.5px] font-medium">
      {(["stdio", "http", "sse"] as const).map((k) => (
        <button
          key={k}
          className={
            "px-3 py-1 rounded-full " +
            (kind === k ? "bg-panel shadow-sm text-ink border border-line" : "text-muted")
          }
          onClick={() => setKind(k)}
        >
          {k === "stdio" ? "Local command" : k === "http" ? "Remote URL" : "SSE URL"}
        </button>
      ))}
    </div>
  );

  return (
    <div className="space-y-2.5 rounded-xl2 border border-line bg-panel p-3.5">
      <div className="flex items-center justify-between">
        <div className="text-[12.5px] font-medium">Add an MCP server</div>
        {KIND}
      </div>
      <input
        className={INPUT}
        placeholder="Server name (e.g. filesystem, gaode)"
        value={name}
        spellCheck={false}
        onChange={(e) => setName(e.target.value)}
      />
      {kind === "stdio" ? (
        <>
          <input
            className={INPUT}
            placeholder="Command (e.g. npx, uvx, python)"
            value={command}
            spellCheck={false}
            onChange={(e) => setCommand(e.target.value)}
          />
          <textarea
            className={INPUT + " resize-y font-mono text-meta"}
            rows={2}
            placeholder={"Args, one per line:\n-y @modelcontextprotocol/server-filesystem /path"}
            value={argsText}
            spellCheck={false}
            onChange={(e) => setArgsText(e.target.value)}
          />
          <textarea
            className={INPUT + " resize-y font-mono text-meta"}
            rows={2}
            placeholder={"Env vars, one per line (optional):\nAPI_KEY=…"}
            value={envText}
            spellCheck={false}
            onChange={(e) => setEnvText(e.target.value)}
          />
        </>
      ) : (
        <>
          <input
            className={INPUT}
            placeholder={kind === "sse" ? "https://…/sse (腾讯云 MCP 广场专属 SSE URL)" : "https://…/mcp (streamable HTTP)"}
            value={url}
            spellCheck={false}
            onChange={(e) => setUrl(e.target.value)}
          />
          <label className="flex items-center gap-2 text-[12.5px] text-muted cursor-pointer">
            <input type="checkbox" checked={oauth} onChange={(e) => setOauth(e.target.checked)} />
            OAuth sign-in (tokens stay on this computer)
          </label>
        </>
      )}
      <div className="flex items-center gap-3">
        <button className={BTN_ACCENT} onClick={save} disabled={busy}>
          {busy ? "Adding…" : "Add & connect"}
        </button>
        {probed != null && !busy && (
          <span className="text-meta text-accent">connected · {probed} tools</span>
        )}
        <button className="text-[12.5px] text-muted hover:text-ink" onClick={onCancel} disabled={busy}>
          cancel
        </button>
      </div>
      <p className="text-[11.5px] text-faint">
        Any MCP server works — Claude / Cursor / Codex-compatible. Claude, OpenWorker, 通义, 混元…
        same standard.
      </p>
    </div>
  );
}

// -- Connectors ---------------------------------------------------------------
// The Connectors tab body moved to connectors/ConnectorsSection.tsx (UX-DECISIONS
// §21: connected-first list + per-connector detail subpages). This file keeps the
// shared building blocks the detail pages reuse: ConnectSetup, ConnectorTools, and
// the two-way blocks (Allowlist/Unauthorized/ListeningSessions).

// Parked messages from senders not on the allow-list (§19). The gateway keeps what they said
// instead of dropping it, so first contact is one step: Allow & deliver replays the original
// message through the normal inbound path — no "message the bot again".
// With `teamId` (the Slack-workspaces page) only that workspace's parked messages show;
// resolving routes the allow to the right workspace server-side (the item carries its team).
export function UnauthorizedBlock({
  c,
  onChanged,
  teamId,
}: {
  c: Connector;
  onChanged: () => void;
  teamId?: string;
}) {
  const items = (c.unauthorized ?? []).filter(
    (m) => teamId === undefined || m.team_id === teamId,
  );
  if (items.length === 0) return null;
  const act = async (id: string, action: "dismiss" | "allow" | "allow_deliver") => {
    await resolveUnauthorized(c.name, id, action);
    onChanged();
  };
  return (
    <div
      className="border-t border-line px-3.5 py-3"
      data-testid={teamId ? `unauthorized-${c.name}-${teamId}` : `unauthorized-${c.name}`}
    >
      <div className={SEC_H + " mb-2"}>
        Messages from senders you haven't allowed · {items.length}
      </div>
      <div className="space-y-2">
        {items.map((m) => (
          <div key={m.id} className="rounded-xl border border-line bg-paper p-2.5">
            <div className="flex items-center gap-2 text-meta text-muted">
              <span className="font-medium text-ink">{m.user_name || m.user_id}</span>
              <span>in {m.chat_name || m.chat_id}</span>
              <span className="ml-auto shrink-0">{relTime(m.ts) || ""}</span>
            </div>
            <div className="text-[12.5px] mt-1 break-words">{m.text}</div>
            <div className="flex items-center gap-1.5 mt-2">
              <button
                className="text-[11.5px] px-2 py-1 rounded-md bg-accent text-white"
                data-testid={`parked-allow-deliver-${m.id}`}
                title="Add the sender to the allow-list and deliver this message now"
                onClick={() => act(m.id, "allow_deliver")}
              >
                Allow & deliver
              </button>
              <button
                className={BTN_BORDERED}
                data-testid={`parked-allow-${m.id}`}
                title="Add the sender to the allow-list; this message is discarded"
                onClick={() => act(m.id, "allow")}
              >
                Allow only
              </button>
              <button
                className="text-[11.5px] px-2 py-1 rounded-md text-faint hover:text-danger"
                data-testid={`parked-dismiss-${m.id}`}
                title="Throw this message away"
                onClick={() => act(m.id, "dismiss")}
              >
                Dismiss
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// Which sessions listen to this connector's channels — the per-connector cut of the global
// Channel-subscriptions table (Integrations ▸ Messaging routing). Subscribing happens from a
// session's Sources ▸ Channels panel; here the owner can see and revoke.
export function ListeningSessionsBlock({ c }: { c: Connector }) {
  const [subs, setSubs] = useState<Subscription[] | null>(null);
  const load = () => getSubscriptions().then(setSubs).catch(() => setSubs([]));
  useEffect(() => {
    load();
  }, [c.name]);
  const platformOf = (channel: string) =>
    channel.includes(":") ? channel.split(":")[0] : "slack";
  const mine = (subs ?? []).filter((s) => platformOf(s.channel) === c.name);
  return (
    <div className="border-t border-line px-3.5 py-3" data-testid={`listening-${c.name}`}>
      <div className={SEC_H + " mb-2"}>Sessions listening to {c.title} channels · {mine.length}</div>
      {mine.length === 0 ? (
        <div className="text-meta text-faint">
          None yet — open a session's Sources ▸ Channels to subscribe it to a channel.
        </div>
      ) : (
        <div className="space-y-1.5">
          {mine.map((s) => (
            <div className="flex items-center gap-2 text-[12.5px]" key={s.session_id + s.channel}>
              <span className="min-w-0 truncate" title={s.session_id}>
                {s.session_title || s.session_id}
                {s.agent ? <span className="text-faint"> · {s.agent}</span> : null}
              </span>
              <span className="text-muted shrink-0" title={s.channel}>
                ← {s.channel_name ? `#${s.channel_name}` : s.channel}
              </span>
              <button
                className="ml-auto text-faint hover:text-danger shrink-0"
                title="Unsubscribe this session"
                onClick={async () => {
                  await unsubscribeChannel(s.session_id, s.channel);
                  load();
                }}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// Who may message this two-way bot. Recent senders surface here once they DM/mention the bot, so you
// can Allow them; allowed users are chips you can remove. (Was orphaned in the super-agent view.)
// With `teamId` (the Slack-workspaces page) the list is that WORKSPACE's — ids are
// workspace-scoped, so allow/remove target `slack:team:<id>` and recents filter to the team.
export function AllowlistBlock({
  c,
  onChanged,
  teamId,
  allowed,
  allowedNames,
}: {
  c: Connector;
  onChanged: () => void;
  teamId?: string;
  allowed?: string[];
  allowedNames?: Record<string, string | null>;
}) {
  const allowedUsers = allowed ?? c.allowed_users;
  const names = allowedNames ?? c.allowed_user_names;
  const recent = (c.recent ?? []).filter(
    (r) => teamId === undefined || r.team_id === teamId,
  );
  const unknownRecent = recent.filter((r) => !r.authorized);

  return (
    <div className="border-t border-line px-3.5 py-3 grid grid-cols-2 gap-5">
      <div>
        <div className={SEC_H + " mb-2"}>Allowed to message</div>
        <div className="flex flex-wrap gap-1.5">
          {allowedUsers.length === 0 && (
            <span className="text-meta text-faint">nobody yet — Allow a recent sender →</span>
          )}
          {allowedUsers.map((u) => (
            <span
              key={u}
              className="inline-flex items-center gap-1.5 pl-2 pr-1 py-1 rounded-full bg-paper border border-line text-meta"
              title={`id ${u}`}
            >
              <span className="w-4 h-4 rounded-full bg-accentSoft text-accent grid place-items-center text-[9px] font-bold">
                {initials(names?.[u] || u)}
              </span>
              {names?.[u] || u}
              <button
                className="w-4 h-4 grid place-items-center text-faint hover:text-danger"
                title="remove"
                onClick={async () => {
                  await disallowUser(c.name, u, teamId);
                  onChanged();
                }}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      </div>
      <div>
        <div className={SEC_H + " mb-2"}>Recent senders</div>
        {unknownRecent.length === 0 ? (
          <div className="text-meta text-faint">None yet. Message the bot once and it'll show here.</div>
        ) : (
          <div className="space-y-1.5">
            {unknownRecent.map((r) => (
              <div className="flex items-center gap-2 text-[12.5px]" key={r.user_id}>
                <span className="w-5 h-5 rounded-full bg-paper border border-line grid place-items-center text-[9px] font-bold text-muted shrink-0">
                  {initials(r.user_name || "?")}
                </span>
                <span className="min-w-0 truncate" title={`id ${r.user_id}`}>
                  {r.user_name || "unknown"} <span className="text-faint">· {r.chat_type}</span>
                </span>
                <button
                  className="ml-auto text-[11.5px] px-2 py-0.5 rounded-md bg-accent text-white shrink-0"
                  onClick={async () => {
                    await allowUser(c.name, r.user_id, teamId);
                    onChanged();
                  }}
                >
                  Allow
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export function ConnectorTools({ c, onChanged }: { c: Connector; onChanged: () => void }) {
  const toggle = async (toolName: string, enabled: boolean) => {
    await updateConnectorTools(c.name, { [toolName]: enabled });
    onChanged();
  };
  if (!c.tools?.length)
    return (
      <div className="border-t border-line px-3.5 py-3 text-[12.5px] text-muted">
        No tools for this connector yet.
      </div>
    );
  return (
    <div className="border-t border-line px-3.5 py-3">
      <div className={SEC_H + " mb-2"}>Tools exposed to OpenWorker</div>
      <div className="space-y-1.5">
        {c.tools.map((tool) => (
          <label
            className="flex items-start gap-2.5 p-2 rounded-lg border border-line bg-paper"
            key={tool.name}
          >
            <input
              type="checkbox"
              className="mt-0.5 shrink-0"
              checked={tool.enabled}
              onChange={(e) => toggle(tool.name, e.target.checked)}
            />
            <span className="min-w-0">
              <span className="block text-ui">{tool.label}</span>
              <span className="block text-[11.5px] text-faint">
                {tool.name} · {tool.kind} · asks approval
              </span>
              <span className="block text-[11.5px] text-faint">{tool.description}</span>
            </span>
          </label>
        ))}
      </div>
    </div>
  );
}

// Exported: also hosted inside the SourcesDrawer's connect-in-context child panel, so a
// recommended connector can be connected without leaving the session (owner ask, 2026-07-03).
export function ConnectSetup({
  c,
  cloud,
  onConnected,
  manualOnly = false,
}: {
  c: Connector;
  cloud: CloudStatus | null;
  onConnected: () => void;
  // The add-modal's Manual pane: the one-click button lives on the sibling
  // pill, so don't render the managed block again here.
  manualOnly?: boolean;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [waiting, setWaiting] = useState(false); // managed flow: browser is open
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    const res = await connectConnector(c.name, values);
    setBusy(false);
    if (res.ok) onConnected();
    else setError(res.error || "could not connect");
  };

  const oneClick = async () => {
    setError(null);
    const res = await connectManaged(c.name);
    // Completion arrives via the tab's poll: the broker form-POSTs the profile
    // to the sidecar, the connector flips to connected, this card closes itself.
    if (res.ok) setWaiting(true);
    else setError(res.error || "could not start managed connect");
  };

  const mcpOneClick = async () => {
    setError(null);
    const res = await connectMcpBacked(c.name);
    // Completion likewise arrives via the poll — the sidecar flips the connector
    // to connected once the local OAuth flow lands.
    if (res.ok) setWaiting(true);
    else setError(res.error || "could not start the connect");
  };

  return (
    <div className="border-t border-line px-3.5 py-3 space-y-3">
      {c.mcp && !manualOnly && (
        /* MCP-backed one-click needs no cloud sign-in — the OAuth flow is local. */
        <div className="space-y-2" data-testid="mcp-connect">
          <button className={BTN_ACCENT} onClick={mcpOneClick} disabled={waiting}>
            {waiting ? "Check your browser…" : `Connect ${c.title} with one click`}
          </button>
          {c.fields.length > 0 && (
            <div className="text-[11.5px] text-faint">or connect manually:</div>
          )}
        </div>
      )}
      {c.managed && !c.mcp && !manualOnly && (
        <div className="space-y-2" data-testid="managed-connect">
          {c.managed_paused ? (
            // One-click temporarily off (e.g. Google pending CASA verification):
            // a visibly-parked button, and the manual path below stays fully live.
            <>
              <button className={BTN_ACCENT + " opacity-50"} disabled data-testid="managed-coming-soon">
                {`Connect ${c.title} with one click`}
                <span className="ml-2 text-label font-medium px-1.5 py-0.5 rounded-full bg-white/25">
                  Coming soon
                </span>
              </button>
              <div className="text-[11.5px] text-faint">
                One-click sign-in is coming soon — connect manually below for now:
              </div>
            </>
          ) : cloud?.signed_in ? (
            <button className={BTN_ACCENT} onClick={oneClick} disabled={waiting}>
              {waiting ? "Check your browser…" : `Connect ${c.title} with one click`}
            </button>
          ) : cloud ? (
            <CloudSignInInline
              blurb={`Sign-in unlocks the one-click ${c.title} connect — or connect manually below.`}
            />
          ) : (
            // Status unknown (fetch pending/failed): never show the sign-in ask to a
            // possibly-signed-in user (FB-013); the host keeps polling.
            <CloudStatusPending />
          )}
          {!c.managed_paused && cloud?.signed_in && (
            <div className="text-[11.5px] text-faint">or connect manually:</div>
          )}
        </div>
      )}
      {c.instructions.length > 0 && (
        <ol className="list-decimal pl-4 text-[12.5px] text-muted leading-relaxed space-y-1">
          {c.instructions.map((step, i) => (
            <li key={i}>{step}</li>
          ))}
        </ol>
      )}
      {c.fields.map((f) => (
        <label className="conn-field" key={f.key}>
          <span className="conn-field-label">
            {f.label}
            {!f.required && <em> (optional)</em>}
          </span>
          <input
            type={f.secret ? "password" : "text"}
            placeholder={f.placeholder}
            value={values[f.key] || ""}
            spellCheck={false}
            onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
          />
          {f.help && <span className="conn-field-help">{f.help}</span>}
        </label>
      ))}
      <div>
        <button className={BTN_ACCENT} onClick={submit} disabled={busy}>
          {busy ? "Validating…" : "Connect"}
        </button>
      </div>
      {error && <div className="text-[12.5px] text-danger">{error}</div>}
    </div>
  );
}
