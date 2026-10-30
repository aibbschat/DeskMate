import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import i18n from "../i18n";
import {
  getSettings,
  getMemorySettings,
  setMemorySettings,
  getTrustedWorkspaces,
  setCompactionSettings,
  setContextBar,
  setHideReasoningRunning,
  setReasoningEffort,
  setOnboarded,
  setPdfSettings,
  setScratchBase,
  setSessionsPeek,
  setWorkspaceTrusted,
  setGovernance,
  getDeskmateGrowth,
  setDeskmateGrowthProactive,
  getDeskmateWillSettings,
  setDeskmateWillSettings,
  getDeskmateAgencyStatus,
  setDeskmateAgencyEnabled,
  runDeskmateAgencyTick,
  type AgencyStatus,
  type WillSettings,
  type CompactionSettings,
  type GovernanceSettings,
  type ModelSettings,
  type PdfSettings,
  type WorkspaceCommandTrust,
} from "../api";
import {
  cancelDictationModelDownload,
  deleteDictationModel,
  downloadDictationModel,
  getAutostart,
  getDictationStatus,
  getKeepAwake,
  checkForUpdate,
  installUpdate,
  isTauri,
  listenDictationDownloadProgress,
  markDictationTestPassed,
  pickFolder,
  setAutostart,
  setKeepAwake,
  startDictation,
  stopDictation,
  verifyDictationModel,
  type DictationDownloadProgress,
  type DictationStatus,
} from "../tauri";
import { useThemePref } from "../theme";
import { useTextSize } from "../textSize";
import { useTurnStyle } from "../turnStyle";
import { Icon } from "./Icon";
import { SkinPicker } from "./deskmate/skins/SkinPicker";
import { LlmChannelsCard } from "./deskmate/LlmChannelsCard";
import { PanelHead } from "./IntegrationsView";
import { ModelsTab } from "./ManageTabs";
import { GalleryModal } from "./GalleryModal";
import { PersonasTab } from "./PersonasTab";
import { showPersonas } from "../flags";

// Settings, restructured (Option 2) into a full-page surface that mirrors IntegrationsView's shell:
// a left sub-nav (Appearance · Files · Models · Personas) + centered panel, replacing the old
// top-tab ManageModal. Local/app concerns live here; anything external (Connectors, Messaging, MCP,
// Activity) stays under Integrations. Appearance + Files are re-skinned to the mock's Tailwind idiom;
// Models + Personas host the existing tab components inside the page shell (field re-skin to follow).
// "appearance" is the General tab's stable key — callers deep-link with it, so the
// rename (UX-021) changed only the label. "files" folded into General as a card.
type SetTab = "appearance" | "models" | "voice" | "personas";

const CARD = "rounded-xl2 border border-line bg-panel";
const FIELD_LABEL = "text-[12.5px] font-medium text-ink";
const FIELD_HELP = "text-meta text-muted mt-1.5 leading-relaxed";
const INPUT =
  "flex-1 min-w-0 px-3 py-2 rounded-lg border border-line bg-paper text-ui text-ink outline-none focus:border-accent";
const BTN_ACCENT = "text-[12.5px] px-3 py-2 rounded-lg bg-accent text-white shrink-0 disabled:opacity-40";
const BTN_BORDERED =
  "text-[12.5px] px-3 py-2 rounded-lg border border-line bg-paper hover:border-lineStrong shrink-0";

const SET_TABS: { key: SetTab; labelKey: string; icon: "sliders" | "code" | "mic" | "sparkle" }[] = [
  { key: "appearance", labelKey: "settings.general", icon: "sliders" },
  { key: "models", labelKey: "settings.models", icon: "code" },
  { key: "voice", labelKey: "settings.voiceInput", icon: "mic" },
  { key: "personas", labelKey: "settings.personas", icon: "sparkle" },
];

export function SettingsView({
  initialTab,
  onOpenPersona,
}: {
  initialTab?: SetTab;
  onOpenPersona?: (id: string) => void;
}) {
  const { t } = useTranslation();
  // Personas is flag-gated (hidden for launch) — filter the tab AND coerce a stale
  // deep-link to it (openSettings("personas") callers) so the page never opens on a
  // section with no nav entry.
  const personas = showPersonas();
  const tabs = personas ? SET_TABS : SET_TABS.filter((tabItem) => tabItem.key !== "personas");
  const wanted = initialTab && (personas || initialTab !== "personas") ? initialTab : "appearance";
  const [tab, setTab] = useState<SetTab>(wanted);

  return (
    <main className="flex-1 min-w-0 flex bg-paper">
      <nav className="page-subnav w-[208px] shrink-0 border-r border-line bg-panel/40 px-3 py-4">
        <div className="px-2 text-[13.5px] font-semibold mb-3 flex items-center gap-2">
          <Icon name="gear" size={16} /> {t("settings.title")}
        </div>
        {tabs.map((tabItem) => {
          const active = tab === tabItem.key;
          return (
            <button
              key={tabItem.key}
              className={
                "w-full text-left px-2.5 py-2 rounded-lg text-ui flex items-center gap-2 " +
                (active ? "bg-paper text-accent font-medium" : "text-muted hover:bg-paper hover:text-ink")
              }
              onClick={() => setTab(tabItem.key)}
            >
              <Icon name={tabItem.icon} size={15} /> {t(tabItem.labelKey)}
            </button>
          );
        })}
      </nav>

      <div className="flex-1 min-w-0 overflow-y-auto hairline-scroll">
        <div className="max-w-3xl mx-auto px-7 py-6">
          {tab === "appearance" ? (
            <AppearanceSection />
          ) : tab === "models" ? (
            <section>
              <PanelHead
                title={t("settings.models")}
                sub={t("settings.modelsSub")}
              />
              <ModelsTab />
              {/* Token savings is model-spend behavior, so it lives here (UX-021),
                  not under General. */}
              <div className="mt-6">
                <TokenSavingsCard />
                <CompactionCard />
                <LlmChannelsCard />
                <WillModulationCard />
                <AgencyCard />
              </div>
            </section>
          ) : tab === "voice" ? (
            <VoiceInputSection />
          ) : (
            <PersonasSection onOpenPersona={onOpenPersona} />
          )}
        </div>
      </div>
    </main>
  );
}

// -- Voice input: deliberate model provisioning + compatibility + microphone test (§37) --------
const voiceError = (error: unknown) =>
  error instanceof Error ? error.message : typeof error === "string" ? error : i18n.t("settings.voiceErrorFallback");

const formatBytes = (bytes: number) => {
  if (!bytes) return "0 MiB";
  return `${Math.round(bytes / 1024 / 1024)} MiB`;
};

function VoiceInputSection() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<DictationStatus | null>(null);
  const [progress, setProgress] = useState<DictationDownloadProgress | null>(null);
  const [phase, setPhase] = useState<"idle" | "downloading" | "verifying" | "testing" | "transcribing">("idle");
  const [error, setError] = useState<string | null>(null);
  const [testTranscript, setTestTranscript] = useState("");
  const desktop = isTauri();

  const publish = (next: DictationStatus) => {
    setStatus(next);
    window.dispatchEvent(new CustomEvent("coworker:voice-input-changed", { detail: next }));
  };

  useEffect(() => {
    if (!desktop) return;
    let active = true;
    let unlisten = () => {};
    void listenDictationDownloadProgress((next) => {
      if (active) setProgress(next);
    }).then((stop) => {
      unlisten = stop;
    });
    void getDictationStatus().then(async (initial) => {
      if (!active || !initial) return;
      publish(initial);
      // One-time migration for models installed by the first STT cut, before verification markers.
      if (initial.model_installed && !initial.model_verified) {
        setPhase("verifying");
        try {
          const verified = await verifyDictationModel();
          if (active) publish(verified);
        } catch (verifyError) {
          if (active) setError(voiceError(verifyError));
        } finally {
          if (active) setPhase("idle");
        }
      }
    });
    return () => {
      active = false;
      unlisten();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [desktop]);

  const download = async () => {
    setError(null);
    setProgress({ downloaded_bytes: 0, total_bytes: status?.model_bytes || 0 });
    setPhase("downloading");
    try {
      publish(await downloadDictationModel());
    } catch (downloadError) {
      setError(voiceError(downloadError));
      const latest = await getDictationStatus();
      if (latest) publish(latest);
    } finally {
      setPhase("idle");
    }
  };

  const cancelDownload = async () => {
    await cancelDictationModelDownload().catch(() => undefined);
  };

  const repair = async () => {
    setError(null);
    try {
      publish(await deleteDictationModel());
      await download();
    } catch (repairError) {
      setError(voiceError(repairError));
    }
  };

  const remove = async () => {
    if (!window.confirm(t("settings.deleteWhisperConfirm"))) return;
    setError(null);
    try {
      publish(await deleteDictationModel());
      setTestTranscript("");
      setProgress(null);
    } catch (deleteError) {
      setError(voiceError(deleteError));
    }
  };

  const toggleTest = async () => {
    if (!status?.supported || !status.model_verified) return;
    setError(null);
    try {
      if (status.recording) {
        setPhase("transcribing");
        const transcript = (await stopDictation()).trim();
        setTestTranscript(transcript);
        if (!transcript) throw new Error(t("settings.noSpeechDetected"));
        publish(await markDictationTestPassed());
      } else {
        setTestTranscript("");
        setPhase("testing");
        publish(await startDictation());
      }
    } catch (testError) {
      setError(voiceError(testError));
      const latest = await getDictationStatus();
      if (latest) publish(latest);
    } finally {
      setPhase("idle");
    }
  };

  const downloading = phase === "downloading" || !!status?.download_in_progress;
  const progressTotal = progress?.total_bytes || status?.model_bytes || 1;
  const progressPercent = Math.min(100, Math.round(((progress?.downloaded_bytes || 0) / progressTotal) * 100));
  const ready = !!status?.supported && !!status?.model_verified && !!status?.test_passed;

  return (
    <section>
      <PanelHead
        title={t("settings.voiceInput")}
        sub={t("settings.voiceInputSub")}
      />

      {!desktop ? (
        <div className={CARD + " p-4 text-ui text-muted"}>{t("settings.voiceInputDesktopOnly")}</div>
      ) : (
        <div className="space-y-4">
          <div className="rounded-xl border border-green-200 bg-green-50/70 px-4 py-3 text-[12.5px] text-green-800">
            <span className="font-medium">{t("settings.privateByDesign")}</span> {t("settings.privateByDesignFull")}
          </div>

          <div className={CARD}>
            <div className="p-4 flex items-start gap-3">
              <Icon name="code" size={18} className="text-accent mt-0.5" />
              <div className="min-w-0 flex-1">
                <div className="text-[13.5px] font-medium">{t("settings.thisDevice")}</div>
                <div className="text-meta text-muted mt-1">{status?.device_summary || t("settings.checkingCompatibility")}</div>
                {status?.compatibility_reason && <div className="text-meta text-red-600 mt-1.5">{status.compatibility_reason}</div>}
              </div>
              {status && (
                <span className={"text-[11.5px] px-2 py-1 rounded-full " + (status.supported ? "bg-green-50 text-green-700" : "bg-red-50 text-red-600")}>
                  {status.supported ? t("settings.compatible") : t("settings.unsupported")}
                </span>
              )}
            </div>
            <div className="border-t border-line bg-paper/50 px-4 py-3 grid grid-cols-2 gap-3 text-meta text-muted">
              <div><span className="block text-ink font-medium">{t("settings.macLabel")}</span>{t("settings.macRequirements")}</div>
              <div><span className="block text-ink font-medium">{t("settings.windowsLabel")}</span>{t("settings.windowsRequirements")}</div>
              <div><span className="block text-ink font-medium">{t("settings.memoryLabel")}</span>{t("settings.memoryRequirements")}</div>
              <div><span className="block text-ink font-medium">{t("settings.processorLabel")}</span>{t("settings.processorRequirements")}</div>
            </div>
          </div>

          <div className={CARD}>
            <div className="p-4 flex items-center gap-3">
              <div className="w-9 h-9 rounded-lg bg-accentSoft text-accent grid place-items-center font-semibold">W</div>
              <div className="min-w-0 flex-1">
                <div className="text-[13.5px] font-medium">{t("settings.whisperBaseEnglish")}</div>
                <div className="text-meta text-muted mt-0.5">
                  {status?.model_verified ? t("settings.installedAndVerified", { size: formatBytes(status.model_bytes) }) : t("settings.localVoiceModel", { size: formatBytes(status?.model_bytes || 147_964_211) })}
                </div>
              </div>
              {status?.model_verified ? (
                <>
                  <span className="text-[11.5px] px-2 py-1 rounded-full bg-green-50 text-green-700">{t("settings.verified")}</span>
                  <button className={BTN_BORDERED} onClick={() => void repair()}>{t("settings.repair")}</button>
                  <button className="text-meta text-red-600 px-2 py-2" onClick={() => void remove()}>{t("settings.delete")}</button>
                </>
              ) : downloading ? (
                <button className={BTN_BORDERED} onClick={() => void cancelDownload()}>{t("settings.cancel")}</button>
              ) : phase === "verifying" ? (
                <span className="text-meta text-muted">{t("settings.verifying")}</span>
              ) : (
                <button className={BTN_ACCENT} disabled={!status?.supported} onClick={() => void download()}>{t("settings.downloadModel")}</button>
              )}
            </div>
            {downloading && (
              <div className="border-t border-line px-4 py-3">
                <div className="h-1.5 rounded-full bg-line overflow-hidden"><div className="h-full bg-accent transition-all" style={{ width: `${progressPercent}%` }} /></div>
                <div className="mt-1.5 text-[11.5px] text-muted flex"><span>{formatBytes(progress?.downloaded_bytes || 0)} of {formatBytes(progressTotal)}</span><span className="ml-auto">{progressPercent}%</span></div>
              </div>
            )}
          </div>

          <div className={CARD}>
            <div className="p-4 flex items-center gap-3">
              <Icon name="mic" size={18} className={ready ? "text-green-600" : "text-muted"} />
              <div className="min-w-0 flex-1">
                <div className="text-[13.5px] font-medium">{t("settings.microphoneTest")}</div>
                <div className="text-meta text-muted mt-0.5">
                  {ready ? t("settings.micReady") : t("settings.micSetup")}
                </div>
              </div>
              {ready && <span className="text-[11.5px] px-2 py-1 rounded-full bg-green-50 text-green-700">{t("settings.ready")}</span>}
              <button className={BTN_BORDERED} disabled={!status?.supported || !status?.model_verified || phase === "transcribing"} onClick={() => void toggleTest()}>
                {status?.recording ? t("settings.stopAndCheck") : phase === "transcribing" ? t("settings.transcribingStatus") : ready ? t("settings.testAgain") : t("settings.testMicrophone")}
              </button>
            </div>
            {status?.recording && <div className="border-t border-line px-4 py-3 text-meta text-accent" role="status">{t("settings.listeningSpeak")}</div>}
            {testTranscript && <div className="border-t border-line bg-paper/50 px-4 py-3 text-ui}">&ldquo;{testTranscript}&rdquo;</div>}
          </div>

          {error && <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2.5 text-meta text-red-700">{error}</div>}
        </div>
      )}
    </section>
  );
}

// -- Personas: installed/enabled/delete management, the dir/Git importer, and the
// entry point to the Persona Gallery (a screen-sized modal — installs finish back
// here, disabled pending consent; a gallery install re-mounts the list in place).
function PersonasSection({ onOpenPersona }: { onOpenPersona?: (id: string) => void }) {
  const { t } = useTranslation();
  const [galleryBump, setGalleryBump] = useState(0);
  const [galleryOpen, setGalleryOpen] = useState(false);

  return (
    <section>
      <PanelHead
        title={t("settings.personas")}
        sub={t("settings.personasSub")}
      />
      <PersonasTab key={galleryBump} onOpenPersona={onOpenPersona} />
      <button
        className="mt-6 w-full rounded-xl2 border border-line bg-panel px-4 py-3.5 flex items-center gap-3 text-left hover:border-lineStrong"
        data-testid="gallery-link"
        onClick={() => setGalleryOpen(true)}
      >
        <Icon name="sparkle" size={16} className="text-accent shrink-0" />
        <span className="min-w-0 flex-1">
          <span className="block text-[13.5px] font-medium">{t("settings.browsePersonaGallery")}</span>
          <span className="block text-meta text-muted">
            {t("settings.browsePersonaGallerySub")}
          </span>
        </span>
        <span className="text-[12.5px] text-accent shrink-0">{t("settings.openArrow")}</span>
      </button>
      {galleryOpen && (
        <GalleryModal
          onClose={() => setGalleryOpen(false)}
          onInstalled={() => setGalleryBump((b) => b + 1)}
        />
      )}
    </section>
  );
}

// -- Appearance + app behaviour ------------------------------------------------
function AppearanceSection() {
  const { t } = useTranslation();
  const [theme, setTheme] = useThemePref();
  const [textSize, setTextSize] = useTextSize();
  const [turnStyle, setTurnStyle] = useTurnStyle();
  const [autostart, setAuto] = useState(false);
  const [keepAwake, setKeep] = useState(false);
  const desktop = isTauri();
  // 桌宠大小（localStorage 持久化，DeskMateOverlay 读取并实时响应）
  const [petScale, setPetScale] = useState<number>(() => {
    const v = parseFloat(localStorage.getItem("deskmate_pet_scale") || "");
    return Number.isFinite(v) && v >= 0.5 && v <= 2 ? v : 1.0;
  });

  useEffect(() => {
    if (isTauri()) {
      getAutostart().then((v) => setAuto(!!v)).catch(() => {});
      getKeepAwake().then((v) => setKeep(!!v)).catch(() => {});
    }
  }, []);

  const toggleAuto = async (v: boolean) => setAuto(!!(await setAutostart(v)));
  const toggleKeep = async (v: boolean) => setKeep(!!(await setKeepAwake(v)));
  const runSetupAgain = async () => {
    await setOnboarded(false);
    window.dispatchEvent(new CustomEvent("coworker:open-onboarding"));
  };

  return (
    <section>
      <PanelHead title={t("settings.general")} sub={t("settings.generalSub")} />

      <div className={CARD + " p-4 mb-4"}>
        <div className={FIELD_LABEL}>{t("settings.theme")}</div>
        <div className="seg mt-2.5" role="radiogroup" aria-label={t("settings.appearanceAria")}>
          {(["light", "dark", "auto"] as const).map((p) => (
            <button key={p} className={p === theme ? "active" : ""} onClick={() => setTheme(p)}>
              {p === "light" ? t("settings.light") : p === "dark" ? t("settings.dark") : t("settings.auto")}
            </button>
          ))}
        </div>
        <div className={FIELD_HELP}>{t("settings.autoHelp")}</div>
      </div>

      {/* 字号档位（按设备生效，localStorage）：只换字号、不改布局宽度 */}
      <div className={CARD + " p-4 mb-4"} data-testid="text-size-card">
        <div className={FIELD_LABEL}>{t("settings.textSize")}</div>
        <div
          className="seg mt-2.5"
          role="radiogroup"
          aria-label={t("settings.textSizeAria")}
          data-testid="text-size-seg"
        >
          {(["small", "default", "large"] as const).map((s) => (
            <button
              key={s}
              className={s === textSize ? "active" : ""}
              data-testid={`text-size-${s}`}
              onClick={() => setTextSize(s)}
            >
              {t(`settings.textSize_${s}`)}
            </button>
          ))}
        </div>
        <div className={FIELD_HELP}>{t("settings.textSizeHelp")}</div>
      </div>

      {/* 思考与执行显示风格（本 fork 新增，与皮肤正交，不改小桔形象） */}
      <div className={CARD + " p-4 mb-4"} data-testid="turn-style-card">
        <div className={FIELD_LABEL}>{t("settings.turn_style_title")}</div>
        <div
          className="seg mt-2.5"
          role="radiogroup"
          aria-label={t("settings.turn_style_title")}
          data-testid="turn-style-seg"
        >
          {(["timeline", "cards", "bubble"] as const).map((s) => (
            <button
              key={s}
              className={s === turnStyle ? "active" : ""}
              data-testid={`turn-style-${s}`}
              onClick={() => setTurnStyle(s)}
            >
              {t(`settings.turn_style_${s}`)}
            </button>
          ))}
        </div>
        <div className={FIELD_HELP}>{t("settings.turn_style_desc")}</div>
      </div>

      {/* 桌宠大小（仅「桌搭 DeskMate」模式生效） */}
      <div className={CARD + " p-4 mb-4"}>
        <div className={FIELD_LABEL + " mb-2.5"}>桌宠大小</div>
        <div className="flex items-center gap-3">
          <input
            type="range"
            min={0.5}
            max={2}
            step={0.05}
            value={petScale}
            onChange={(e) => {
              const v = parseFloat(e.target.value);
              setPetScale(v);
              localStorage.setItem("deskmate_pet_scale", String(v));
              window.dispatchEvent(new CustomEvent("deskmate:pet-scale", { detail: v }));
            }}
            className="flex-1"
          />
          <span className="text-meta tabular-nums text-muted w-10 text-right">
            {Math.round(petScale * 100)}%
          </span>
        </div>
        <div className={FIELD_HELP}>调整桌面桌宠（小桔）的显示大小，仅「小桔」模式下可见。</div>
      </div>

      <SkinPicker />

      <SidebarCard />

      <ContextBarCard />

      <MemoryInjectionCard />

      <TrustProactiveCard />

      <FilesCard />

      <SafetyCard />

      <TrustedWorkspacesCard />

      {desktop && (
        <div className={CARD + " p-4"}>
          <div className={FIELD_LABEL + " mb-2.5"}>{t("settings.alwaysOn")}</div>
          <label className="flex items-start gap-3 py-2">
            <input type="checkbox" className="mt-0.5" checked={autostart} onChange={(e) => toggleAuto(e.target.checked)} />
            <span>
              <span className="block text-ui text-ink">{t("settings.openAtLogin")}</span>
              <span className="block text-meta text-muted">{t("settings.openAtLoginDesc")}</span>
            </span>
          </label>
          <label className="flex items-start gap-3 py-2">
            <input type="checkbox" className="mt-0.5" checked={keepAwake} onChange={(e) => toggleKeep(e.target.checked)} />
            <span>
              <span className="block text-ui text-ink">{t("settings.keepAwake")}</span>
              <span className="block text-meta text-muted">{t("settings.keepAwakeDesc")}</span>
            </span>
          </label>
        </div>
      )}

      {/* One card for the app-lifecycle actions (UX-021): the onboarding replay (§24 —
          every build, the browser dev shell runs the same first-run flow) and, on
          desktop, the manual update check (launch also checks automatically). */}
      <div className={CARD + " p-4 mt-4"}>
        <div className={FIELD_LABEL + " mb-2"}>{t("settings.setupAndUpdates")}</div>
        <div className="flex items-center gap-2">
          <button className={BTN_BORDERED} onClick={runSetupAgain}>
            {t("settings.runSetupAgain")}
          </button>
          {desktop && <UpdateInline />}
        </div>
        <div className={FIELD_HELP}>{t("settings.setupHelp")}</div>
      </div>
    </section>
  );
}

function TrustedWorkspacesCard() {
  const { t } = useTranslation();
  const [workspaces, setWorkspaces] = useState<WorkspaceCommandTrust[] | null>(null);

  const refresh = () =>
    getTrustedWorkspaces()
      .then(setWorkspaces)
      .catch(() => setWorkspaces([]));

  useEffect(() => {
    refresh();
  }, []);

  const revoke = async (path: string) => {
    if (!window.confirm(t("settings.revokeCommandTrustConfirm", { path }))) return;
    await setWorkspaceTrusted(path, false);
    refresh();
  };

  return (
    <div className={CARD + " p-4 mb-4"} data-testid="trusted-workspaces-card">
      <div className={FIELD_LABEL}>{t("settings.trustedWorkspaces")}</div>
      <div className={FIELD_HELP}>
        {t("settings.trustedWorkspacesHelp")}
      </div>
      {workspaces === null ? (
        <div className="text-meta text-muted mt-3">{t("settings.loading")}</div>
      ) : workspaces.length === 0 ? (
        <div className="text-meta text-muted mt-3">{t("settings.noWorkspacesTrusted")}</div>
      ) : (
        <div className="mt-3 divide-y divide-line">
          {workspaces.map((workspace) => (
            <div key={workspace.workspace} className="py-2.5 flex items-start gap-3">
              <div className="min-w-0 flex-1">
                <div className="text-[12.5px] text-ink break-all">{workspace.workspace}</div>
                <div className="text-[11.5px] text-muted mt-0.5">
                  {workspace.requested_commands.length
                    ? `${workspace.requested_commands.length}${workspace.requested_commands.length === 1 ? t("settings.projectCommandAllowance") : t("settings.projectCommandAllowances")}`
                    : t("settings.noProjectCommandAllowances")}
                  {!workspace.exists ? t("settings.folderUnavailable") : ""}
                </div>
              </div>
              <button
                className="text-meta text-red-600 px-2 py-1"
                onClick={() => void revoke(workspace.workspace)}
              >
                {t("settings.revoke")}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// -- Safety & sandbox (governance): execution level + sandbox toggles ---------
// Execution level is hot-applied to running sessions; sandbox flags take effect for
// new sessions (they wrap the executor process at build time).
function SafetyCard() {
  const { t } = useTranslation();
  const [gov, setGov] = useState<GovernanceSettings | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getSettings()
      .then((s) =>
        setGov({
          execution_level: s.execution_level || "auto",
          sandbox_enabled: s.sandbox_enabled !== false,
          sandbox_network: s.sandbox_network !== false,
          sandbox_deny_paths: s.sandbox_deny_paths || [],
        }),
      )
      .catch(() =>
        setGov({
          execution_level: "auto",
          sandbox_enabled: true,
          sandbox_network: true,
          sandbox_deny_paths: [],
        }),
      );
  }, []);

  const save = async (patch: Partial<GovernanceSettings>) => {
    if (!gov) return;
    const next = { ...gov, ...patch };
    setGov(next);
    setBusy(true);
    try {
      await setGovernance(patch);
    } finally {
      setBusy(false);
    }
  };

  if (!gov) return null;
  return (
    <div className={CARD + " p-4 mb-4"} data-testid="safety-card">
      <div className={FIELD_LABEL}>{t("settings.safety")}</div>
      <div className={FIELD_HELP}>{t("settings.safetyHelp")}</div>

      <div className="mt-3 text-ui text-ink">{t("settings.executionLevel")}</div>
      <div className="seg mt-2" role="radiogroup" aria-label={t("settings.executionLevel")} data-testid="execution-level">
        {(["off", "auto", "strict"] as const).map((lv) => (
          <button
            key={lv}
            className={gov.execution_level === lv ? "active" : ""}
            disabled={busy}
            onClick={() => save({ execution_level: lv })}
          >
            {t(`settings.level${lv[0].toUpperCase()}${lv.slice(1)}`)}
          </button>
        ))}
      </div>
      <div className={FIELD_HELP}>{t("settings.executionLevelHelp")}</div>

      <label className="flex items-start gap-3 py-2.5">
        <input
          type="checkbox"
          className="mt-0.5"
          data-testid="sandbox-enabled"
          checked={gov.sandbox_enabled}
          disabled={busy}
          onChange={(e) => save({ sandbox_enabled: e.target.checked })}
        />
        <span>
          <span className="block text-ui text-ink">{t("settings.sandboxEnabled")}</span>
          <span className="block text-meta text-muted">{t("settings.sandboxEnabledHelp")}</span>
        </span>
      </label>

      <label className="flex items-start gap-3 py-2.5">
        <input
          type="checkbox"
          className="mt-0.5"
          data-testid="sandbox-network"
          checked={gov.sandbox_network}
          disabled={busy || !gov.sandbox_enabled}
          onChange={(e) => save({ sandbox_network: e.target.checked })}
        />
        <span>
          <span className="block text-ui text-ink">{t("settings.sandboxNetwork")}</span>
          <span className="block text-meta text-muted">{t("settings.sandboxNetworkHelp")}</span>
        </span>
      </label>

      <div className={FIELD_HELP}>{t("settings.sandboxAppliesNew")}</div>
    </div>
  );
}

function UpdateInline() {
  const { t } = useTranslation();
  const [state, setState] = useState<"idle" | "checking" | "none" | "found" | "installing" | "error">("idle");
  const [version, setVersion] = useState("");

  const check = async () => {
    setState("checking");
    try {
      const u = await checkForUpdate();
      if (u) {
        setVersion(u.version);
        setState("found");
      } else {
        setState("none");
      }
    } catch {
      setState("error");
    }
  };

  const install = async () => {
    setState("installing");
    try {
      await installUpdate(); // success restarts the app
    } catch {
      setState("error");
    }
  };

  const statusText =
    state === "none"
      ? t("settings.latestVersion")
      : state === "error"
        ? t("settings.couldNotCheck")
        : t("settings.downloadingRestart");

  return (
    <span className="inline-flex items-center gap-2.5">
      {state === "found" ? (
        <button className={BTN_BORDERED} onClick={install} data-testid="settings-update-install">
          {t("settings.updateToVersion", { version })}
        </button>
      ) : (
        <button
          className={BTN_BORDERED}
          onClick={check}
          disabled={state === "checking" || state === "installing"}
          data-testid="settings-update-check"
        >
          {state === "checking" ? t("settings.checking") : t("settings.checkForUpdates")}
        </button>
      )}
      {(state === "none" || state === "error" || state === "installing") && (
        <span className="text-meta text-muted">
          {statusText}
        </span>
      )}
    </span>
  );
}

// Telemetry/Privacy card removed for this release (owner ask 2026-07-22); the
// setCloudTelemetry API stays for a future opt-out surface.

// -- Sidebar density -------------------------------------------------------------
// -- Token savings (PDF attachments; owner ask, 2026-07-17) ---------------------
// Attachments replay with EVERY turn, so a big PDF quietly multiplies token spend.
// This card is the attachment dial: attach thresholds + the fallback for models
// without native PDF support. (Long-history spend is handled by auto-compaction —
// the CompactionCard below, OPE-27.)
function TokenSavingsCard() {
  const { t } = useTranslation();
  const [pdf, setPdf] = useState<PdfSettings | null>(null);

  useEffect(() => {
    getSettings()
      .then((s) =>
        setPdf({
          pdf_fallback: s.pdf_fallback || "text",
          pdf_max_pages: s.pdf_max_pages || 20,
          pdf_max_mb: s.pdf_max_mb || 10,
        }),
      )
      .catch(() => setPdf({ pdf_fallback: "text", pdf_max_pages: 20, pdf_max_mb: 10 }));
  }, []);

  const save = async (patch: Partial<PdfSettings>) => {
    setPdf((p) => (p ? { ...p, ...patch } : p));
    await setPdfSettings(patch);
  };

  if (!pdf) return null;
  return (
    <div className={CARD + " p-4 mb-4"} data-testid="token-savings-card">
      <div className={FIELD_LABEL}>{t("settings.tokenSavings")}</div>
      <div className={FIELD_HELP}>
        {t("settings.tokenSavingsHelp")}
      </div>

      <div className="mt-3 text-ui text-ink">{t("settings.pdfsNoNativePdf")}</div>
      <div className="seg mt-2" role="radiogroup" aria-label={t("settings.pdfFallbackAria")} data-testid="pdf-fallback">
        <button
          className={pdf.pdf_fallback === "text" ? "active" : ""}
          onClick={() => save({ pdf_fallback: "text" })}
        >
          {t("settings.extractText")}
        </button>
        <button
          className={pdf.pdf_fallback === "images" ? "active" : ""}
          onClick={() => save({ pdf_fallback: "images" })}
        >
          {t("settings.sendPageImages")}
        </button>
      </div>
      <div className={FIELD_HELP}>
        {t("settings.pdfFallbackHelp")}
      </div>

      <div className="mt-3 flex items-center gap-5">
        <label className="flex items-center gap-2.5">
          <span className="text-ui text-ink">{t("settings.maxPages")}</span>
          <input
            type="number"
            min={1}
            max={100}
            value={pdf.pdf_max_pages}
            data-testid="pdf-max-pages"
            className="w-16 px-2 py-1.5 rounded-lg border border-line bg-paper text-ui text-ink outline-none focus:border-accent"
            onChange={(e) => save({ pdf_max_pages: Math.max(1, Math.min(Number(e.target.value) || 20, 100)) })}
          />
        </label>
        <label className="flex items-center gap-2.5">
          <span className="text-ui text-ink">{t("settings.maxSize")}</span>
          <input
            type="number"
            min={1}
            max={10}
            value={pdf.pdf_max_mb}
            data-testid="pdf-max-mb"
            className="w-16 px-2 py-1.5 rounded-lg border border-line bg-paper text-ui text-ink outline-none focus:border-accent"
            onChange={(e) => save({ pdf_max_mb: Math.max(1, Math.min(Number(e.target.value) || 10, 10)) })}
          />
          <span className="text-[12.5px] text-muted">{t("settings.mb")}</span>
        </label>
      </div>
      <div className={FIELD_HELP}>
        {t("settings.pdfLimitsHelp")}
      </div>
    </div>
  );
}

// -- Context compaction (OPE-27) ------------------------------------------------
// Long sessions are summarized automatically when they approach the model's context
// limit, so work continues instead of hitting a raw provider error. Two spec'd
// overrides (trigger % + token cap) and the summarizer-model pin — nothing more.
function CompactionCard() {
  const [cfg, setCfg] = useState<CompactionSettings | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [labels, setLabels] = useState<Record<string, string>>({});
  // Uncommitted keystrokes for the two numeric fields. Saving on every keystroke would
  // clamp partial input to the minimum ("1" of "100000" → 10_000 persisted instantly),
  // silently crippling the compaction trigger. Commit on blur / Enter instead.
  const [draftPct, setDraftPct] = useState<string | null>(null);
  const [draftCap, setDraftCap] = useState<string | null>(null);

  useEffect(() => {
    getSettings()
      .then((s) => {
        setCfg({
          compaction_threshold_pct: s.compaction_threshold_pct ?? 0.8,
          compaction_cap_tokens: s.compaction_cap_tokens ?? 250_000,
          compaction_model: s.compaction_model ?? "",
        });
        setModels(s.models || []);
        setLabels(s.model_labels || {});
      })
      .catch(() =>
        setCfg({
          compaction_threshold_pct: 0.8,
          compaction_cap_tokens: 250_000,
          compaction_model: "",
        }),
      );
  }, []);

  const save = async (patch: Partial<CompactionSettings>) => {
    setCfg((p) => (p ? { ...p, ...patch } : p));
    await setCompactionSettings(patch);
  };

  if (!cfg) return null;
  const modelLabel = (id: string) => labels[id]?.split(" · ")[0] || id;
  const pctValue = Math.round(cfg.compaction_threshold_pct * 100);
  const commitPct = () => {
    if (draftPct === null) return;
    const n = Number(draftPct);
    const next =
      draftPct.trim() === "" || !Number.isFinite(n) ? pctValue : Math.max(10, Math.min(n, 95));
    setDraftPct(null);
    if (next !== pctValue) save({ compaction_threshold_pct: next / 100 });
  };
  const commitCap = () => {
    if (draftCap === null) return;
    const n = Number(draftCap);
    const next =
      draftCap.trim() === "" || !Number.isFinite(n)
        ? cfg.compaction_cap_tokens
        : Math.max(10_000, Math.min(n, 2_000_000));
    setDraftCap(null);
    if (next !== cfg.compaction_cap_tokens) save({ compaction_cap_tokens: next });
  };
  return (
    <div className={CARD + " p-4 mb-4"} data-testid="compaction-card">
      <div className={FIELD_LABEL}>Context compaction</div>
      <div className={FIELD_HELP}>
        Long sessions are compacted automatically: older turns are summarized so the
        coworker keeps working instead of running out of context. Your visible transcript
        is never changed — a small marker shows where compaction happened.
      </div>

      <div className="mt-3 flex items-center gap-5 flex-wrap">
        <label className="flex items-center gap-2.5">
          <span className="text-ui text-ink">Compact at</span>
          <input
            type="number"
            min={10}
            max={95}
            value={draftPct ?? pctValue}
            data-testid="compaction-threshold"
            className="w-16 px-2 py-1.5 rounded-lg border border-line bg-paper text-ui text-ink outline-none focus:border-accent"
            onChange={(e) => setDraftPct(e.target.value)}
            onBlur={commitPct}
            onKeyDown={(e) => {
              if (e.key === "Enter") e.currentTarget.blur();
              if (e.key === "Escape") setDraftPct(null);
            }}
          />
          <span className="text-[12.5px] text-muted">% of the context window</span>
        </label>
        <label className="flex items-center gap-2.5">
          <span className="text-ui text-ink">or at</span>
          <input
            type="number"
            min={10_000}
            max={2_000_000}
            step={10_000}
            value={draftCap ?? cfg.compaction_cap_tokens}
            data-testid="compaction-cap"
            className="w-28 px-2 py-1.5 rounded-lg border border-line bg-paper text-ui text-ink outline-none focus:border-accent"
            onChange={(e) => setDraftCap(e.target.value)}
            onBlur={commitCap}
            onKeyDown={(e) => {
              if (e.key === "Enter") e.currentTarget.blur();
              if (e.key === "Escape") setDraftCap(null);
            }}
          />
          <span className="text-[12.5px] text-muted">tokens, whichever is smaller</span>
        </label>
      </div>
      <div className={FIELD_HELP}>
        The cap makes very-large-context models compact early — quality and speed degrade
        well before their nominal limit.
      </div>

      <div className="mt-3 flex items-center gap-2.5">
        <span className="text-ui text-ink">Summarizer model</span>
        <select
          value={cfg.compaction_model}
          data-testid="compaction-model"
          className="px-2 py-1.5 rounded-lg border border-line bg-paper text-ui text-ink outline-none focus:border-accent"
          onChange={(e) => save({ compaction_model: e.target.value })}
        >
          <option value="">Session&rsquo;s own model (default)</option>
          {models.map((m) => (
            <option key={m} value={m}>
              {modelLabel(m)}
            </option>
          ))}
        </select>
      </div>
      <div className={FIELD_HELP}>
        The summary is written by this model. The default follows whatever model the
        session is using.
      </div>
    </div>
  );
}

// -- Composer: context-window bar (owner ask 2026-07-30) ------------------------
// The chip's bar is context-window occupancy; the session total (unbounded) lives in
// the popover. Some people would rather not watch a meter at all, hence the toggle.
function ContextBarCard() {
  const { t } = useTranslation();
  const [shown, setShown] = useState<boolean | null>(null);
  const [hidden, setHidden] = useState<boolean | null>(null);
  const [effort, setEffort] = useState<string>("default");

  useEffect(() => {
    getSettings()
      .then((s) => {
        setShown(s.context_bar === true);
        setHidden(s.hide_reasoning_running === true);
        setEffort(s.reasoning_effort || "default");
      })
      .catch(() => {
        setShown(false);
        setHidden(false);
        setEffort("default");
      });
  }, []);

  const save = async (next: boolean) => {
    setShown(next);
    await setContextBar(next);
  };

  const saveHidden = async (next: boolean) => {
    setHidden(next);
    await setHideReasoningRunning(next);
  };

  const saveEffort = async (next: string) => {
    setEffort(next);
    await setReasoningEffort(next);
  };

  if (shown === null || hidden === null) return null;
  return (
    <div className={CARD + " p-4 mb-4"} data-testid="context-bar-card">
      <div className={FIELD_LABEL}>Composer</div>
      <label className="flex items-start gap-3 py-2">
        <input
          type="checkbox"
          className="mt-0.5"
          data-testid="context-bar-toggle"
          checked={shown}
          onChange={(e) => save(e.target.checked)}
        />
        <span>
          <span className="block text-ui text-ink">Show the context window bar</span>
          <span className="block text-meta text-muted">
            A small meter showing how full the model&rsquo;s context window is. Turn it off
            to show this session&rsquo;s token total instead; either way the full breakdown
            is one click away.
          </span>
        </span>
      </label>
      <label className="flex items-start gap-3 py-2">
        <input
          type="checkbox"
          className="mt-0.5"
          data-testid="hide-reasoning-toggle"
          checked={hidden}
          onChange={(e) => saveHidden(e.target.checked)}
        />
        <span>
          <span className="block text-ui text-ink">{t("settings.hide_reasoning_title")}</span>
          <span className="block text-meta text-muted">{t("settings.hide_reasoning_desc")}</span>
        </span>
      </label>
      <div className="flex items-center gap-3 py-2">
        <span className="block text-ui text-ink">{t("settings.reasoning_effort_title")}</span>
        <select
          className="ml-auto text-meta text-ink bg-transparent border border-border rounded px-2 py-1"
          value={effort}
          onChange={(e) => saveEffort(e.target.value)}
          data-testid="reasoning-effort-select"
        >
          <option value="default">{t("settings.reasoning_effort_default")}</option>
          <option value="low">{t("settings.reasoning_effort_low")}</option>
          <option value="medium">{t("settings.reasoning_effort_medium")}</option>
          <option value="high">{t("settings.reasoning_effort_high")}</option>
        </select>
      </div>
    </div>
  );
}

// -- 记忆自动注入（P0-1：默认开启，可在设置中关闭） -----------------------
function MemoryInjectionCard() {
  const [on, setOn] = useState<boolean | null>(null);

  useEffect(() => {
    getMemorySettings()
      .then((s) => setOn(s.auto_inject !== false))
      .catch(() => setOn(true));
  }, []);

  const save = async (next: boolean) => {
    setOn(next);
    await setMemorySettings({ auto_inject: next });
  };

  if (on === null) return null;
  return (
    <div className={CARD + " p-4 mb-4"} data-testid="memory-injection-card">
      <div className={FIELD_LABEL}>记忆自动注入</div>
      <label className="flex items-start gap-3 py-2">
        <input
          type="checkbox"
          className="mt-0.5"
          data-testid="memory-injection-toggle"
          checked={on}
          onChange={(e) => save(e.target.checked)}
        />
        <span>
          <span className="block text-ui text-ink">
            每轮自动带上相关记忆
          </span>
          <span className="block text-meta text-muted">
            开启后，小桔会在每次对话时自动检索并注入与当前话题相关的记忆，越聊越懂你。关闭则保留记忆但不自动注入。
          </span>
        </span>
      </label>
    </div>
  );
}

function TrustProactiveCard() {
  const [on, setOn] = useState<boolean | null>(null);

  useEffect(() => {
    getDeskmateGrowth()
      .then((d) => {
        if (d && typeof d.trust_controls_proactive === "boolean") {
          setOn(d.trust_controls_proactive);
        } else {
          setOn(true);
        }
      })
      .catch(() => setOn(true));
  }, []);

  const save = async (next: boolean) => {
    setOn(next);
    const ok = await setDeskmateGrowthProactive(next);
    if (!ok) setOn(!next);
  };

  if (on === null) return null;
  return (
    <div className={CARD + " p-4 mb-4"} data-testid="trust-proactive-card">
      <div className={FIELD_LABEL}>信任调制主动打扰</div>
      <label className="flex items-start gap-3 py-2">
        <input
          type="checkbox"
          className="mt-0.5"
          data-testid="trust-proactive-toggle"
          checked={on}
          onChange={(e) => save(e.target.checked)}
        />
        <span>
          <span className="block text-x] text-ink">
            信任感影响主动建议的频率
          </span>
          <span className="block text-x] text-muted">
            开启后，小桔对你的信任度越高，主动提供建议越积极；信任度低时会更保守、更少打扰。关闭则按固定阈值主动建议，不受信任度影响。
          </span>
        </span>
      </label>
    </div>
  );
}

function WillModulationCard() {
  const [s, setS] = useState<WillSettings | null>(null);

  useEffect(() => {
    getDeskmateWillSettings().then((v) => {
      if (v && v.thresholds) setS(v);
    });
  }, []);

  const update = async (patch: Partial<WillSettings>) => {
    if (!s) return;
    const next: WillSettings = {
      ...s,
      ...patch,
      thresholds: { ...s.thresholds, ...(patch.thresholds || {}) },
    };
    setS(next);
    const ok = await setDeskmateWillSettings(patch);
    if (!ok) setS(s);
  };

  const updThreshold = (k: keyof WillSettings["thresholds"], v: number) =>
    update({ thresholds: { ...(s?.thresholds || ({} as WillSettings["thresholds"])), [k]: v } });

  if (!s) return null;

  const Row = (props: {
    label: string;
    desc: string;
    testid: string;
    min: number;
    max: number;
    step: number;
    value: number;
    onChange: (n: number) => void;
  }) => (
    <div className="py-2 flex items-start gap-3" data-testid={props.testid}>
      <div className="flex-1">
        <div className="text-ui text-ink">{props.label}</div>
        <div className="text-meta text-muted">{props.desc}</div>
      </div>
      <input
        type="number"
        min={props.min}
        max={props.max}
        step={props.step}
        value={props.value}
        onChange={(e) => {
          const n = Number(e.target.value);
          if (Number.isFinite(n)) props.onChange(n);
        }}
        className="w-20 px-2 py-1 rounded-lg border border-line bg-paper text-ui text-ink outline-none focus:border-accent"
      />
    </div>
  );

  return (
    <div className={CARD + " p-4 mb-4"} data-testid="will-modulation-card">
      <div className={FIELD_LABEL}>意志层 / 行为调制</div>
      <div className="text-meta text-muted mt-1 leading-relaxed">
        这 5 个阈值决定意志层让「画像」覆盖到「行为」的杠杆长度。每条独立保存。
        <span className="block mt-1">
          <span className="text-accent">📎 联动关系</span>：4 与 5 共用同一条画像输入（你过去的高置信立场）——立场命中数增多时，这两条会自动一起涨。需要压制其中某一条，请直接手动覆盖。
        </span>
        <span className="block mt-1">
          <span className="text-accent">⚠ 越线提示</span>：把 2/4/5 三个拉到上限会让小桔在表达上"很像你"——视角借用变强、可能在语气上替你做主张。这是「成为我」的设计本意，但强度由你手动踩。
        </span>
      </div>
      <label className="flex items-start gap-3 py-2 mt-1">
        <input
          type="checkbox"
          className="mt-0.5"
          data-testid="will-enabled-toggle"
          checked={s.enabled}
          onChange={(e) => update({ enabled: e.target.checked })}
        />
        <span>
          <span className="block text-ui text-ink">让认知与情感影响行为（总开关）</span>
          <span className="block text-meta text-muted">
            关闭 = 按固定参数运行，下方 5 个阈值都不生效；开启 = 下方的值才是实际生效值。修改任何一条后请保持总开关开启，否则不生效。
          </span>
        </span>
      </label>
      <Row
        testid="will-thr-freq-max"
        label="① 情绪负面时主动间隔倍率（×）"
        desc="默认值 1.4，范围 1.0–2.5。读今日情绪层（recent_emotion）。调高=情绪负面时小桔拉长主动间隔更久（更沉默）；调低=趋近 ×1.0，情绪几乎不影响主动。**独立工作**，不与其他条联动。"
        min={1.0}
        max={2.5}
        step={0.05}
        value={s.thresholds.emotional_proactive_frequency_max}
        onChange={(n) => updThreshold("emotional_proactive_frequency_max", n)}
      />
      <Row
        testid="will-thr-th-pos"
        label="② 高立场命中时主动阈值下降量"
        desc="默认值 0.15，范围 0.0–0.4。读认知层 stance_count（≥0.7 高置信立场数量）。调高=小桔在你信任的话题上更主动/更敢接话；调低=立场多少几乎不影响主动。**注意**：靠近 0.4 上限是「最像你」档，此时小桔会表现出以你的判断标准替你想了一遍的态度。"
        min={0}
        max={0.4}
        step={0.01}
        value={s.thresholds.cognitive_threshold_delta_pos}
        onChange={(n) => updThreshold("cognitive_threshold_delta_pos", n)}
      />
      <Row
        testid="will-thr-th-neg"
        label="③ 盲区多时主动阈值上调量"
        desc="默认值 0.0，范围 0.0–0.4。读认知层 open_gap_count（未证伪/未知项）。调高=小桔在不确定的话题上更显著闭嘴；不动=沉默闸门不起作用（默认安全）。**独立工作**，不与其他条联动。**注意**：此条与②共用同一方法 modulate_proactive_threshold，但②+③=总阈值净变化（互为反向），可以分别踩。 "
        min={0}
        max={0.4}
        step={0.01}
        value={s.thresholds.cognitive_threshold_delta_neg}
        onChange={(n) => updThreshold("cognitive_threshold_delta_neg", n)}
      />
      <Row
        testid="will-thr-budget"
        label="④ 价值观 tradeoff 时记忆预算倍率（×）"
        desc="默认值 1.0（不放大），范围 1.0–1.5。读意志层 has_value_signal（你说过「权衡/妥协/优先级/冲突/不行/必须/绝不/底线/宁可/让位/排序/先后」等词）。调高=下次回应能引用的萧哥历史立场/价值排序更多；不动=1.0× 灌入（即不放大额外记忆上下文）。**联动**：与⑤共用同一条画像输入——立场命中增多时，④与⑤会自动一起涨。"
        min={1.0}
        max={1.5}
        step={0.05}
        value={s.thresholds.memory_budget_ratio}
        onChange={(n) => updThreshold("memory_budget_ratio", n)}
      />
      <Row
        testid="will-thr-tone-len"
        label="⑤ 高立场话题时语气追加最大长度（字）"
        desc="默认值 60 字，范围 20–200。读意志层 top_stances（前 3 高置信立场）。调高=小桔在立场话题表达时会复述/援引你的判断（'按你之前的标准…''你曾表达过…'）；调低=立场只用来调行为模式，嘴上不复述。**联动**：立场命中增多时与④自动一起涨。"
        min={20}
        max={200}
        step={10}
        value={s.thresholds.tone_prompt_max_len}
        onChange={(n) => updThreshold("tone_prompt_max_len", n)}
      />
    </div>
  );
}

/**
 * L2 自主层（替你行动）总控卡片。
 *
 * 与意志层（L1，让认知情感影响行为）是两件事：
 * L1 决定「小桔怎么表达」，L2 决定「小桔能不能自己动手」。
 * 所以这里只暴露三样东西：总开关、当前白名单（只读）、立刻跑一次。
 */
function AgencyCard() {
  const [s, setS] = useState<AgencyStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    getDeskmateAgencyStatus().then(setS).catch(() => setS(null));
  }, []);

  const toggle = async (enabled: boolean) => {
    if (!s) return;
    const prev = s;
    setS({ ...s, enabled });
    const ok = await setDeskmateAgencyEnabled(enabled);
    if (!ok) setS(prev);
  };

  const tickOnce = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const res = await runDeskmateAgencyTick();
      if (!res) {
        setMsg("执行失败");
      } else if (res.skipped) {
        setMsg("已关闭，未执行");
      } else {
        const n = Number(res.task_count || 0);
        setMsg(n > 0 ? `完成 ${n} 项` : "本轮无可执行动作（多在冷却中）");
      }
      await getDeskmateAgencyStatus().then(setS);
    } finally {
      setBusy(false);
    }
  };

  if (!s) return null;

  const actions = s.whitelist_actions || [];
  const topics = s.topics || [];

  return (
    <div className={CARD + " p-4 mb-4"} data-testid="agency-card">
      <div className={FIELD_LABEL}>自主层 / 替你行动（L2）</div>
      <div className="text-meta text-muted mt-1 leading-relaxed">
        小桔可以<b>不等你开口</b>就去做的事。低风险动作（如主动信息摄入）自主执行并记进「小桔在忙」；
        <span className="text-accent"> 高风险动作（写文件 / 发消息 / 删除）永远不会自动执行</span>，
        只会生成提议卡等你裁决。
      </div>

      <label className="flex items-start gap-3 py-2 mt-1">
        <input
          type="checkbox"
          className="mt-0.5"
          data-testid="agency-enabled-toggle"
          checked={s.enabled}
          onChange={(e) => toggle(e.target.checked)}
        />
        <span>
          <span className="block text-ui text-ink">允许小桔自主行动（总开关）</span>
          <span className="block text-meta text-muted">
            关闭 = 小桔只回应你，绝不主动做任何事。也可设环境变量 <code>DESKMATE_L2_ENABLED=0</code> 强制关闭。
          </span>
        </span>
      </label>

      <div className="py-2" data-testid="agency-whitelist">
        <div className="text-[12.5px] font-medium text-ink">白名单动作</div>
        <div className="text-meta text-muted mt-1">
          {actions.length === 0 ? (
            "（空）"
          ) : (
            <span className="flex flex-wrap gap-1.5 mt-1">
              {actions.map((a) => (
                <span
                  key={a}
                  className="px-2 py-0.5 rounded-md text-[11.5px] bg-accent/10 text-accent border border-accent/20"
                >
                  {a}
                </span>
              ))}
            </span>
          )}
        </div>
      </div>

      {topics.length > 0 && (
        <div className="py-2" data-testid="agency-topics">
          <div className="text-[12.5px] font-medium text-ink">关注主题域</div>
          <div className="text-meta text-muted mt-1">{topics.join(" · ")}</div>
        </div>
      )}

      <div className="flex items-center gap-3 pt-2">
        <button
          type="button"
          data-testid="agency-tick-btn"
          disabled={busy || !s.enabled}
          onClick={tickOnce}
          className="px-3 py-1.5 rounded-lg border border-line bg-paper text-[12.5px] text-ink hover:border-accent hover:text-accent disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {busy ? "执行中…" : "立刻跑一次"}
        </button>
        {msg && <span className="text-meta text-muted">{msg}</span>}
      </div>
    </div>
  );
}

function SidebarCard() {
  const { t } = useTranslation();
  const [peek, setPeek] = useState<number | null>(null);

  useEffect(() => {
    getSettings()
      .then((s) => setPeek(s.sessions_peek || 5))
      .catch(() => setPeek(5));
  }, []);

  const save = async (n: number) => {
    const clamped = Math.max(1, Math.min(n || 5, 50));
    setPeek(clamped);
    await setSessionsPeek(clamped);
  };

  if (peek === null) return null;
  return (
    <div className={CARD + " p-4 mb-4"}>
      <div className={FIELD_LABEL}>{t("settings.sidebar")}</div>
      <label className="flex items-center gap-3 mt-2.5">
        <span className="text-ui text-ink">{t("settings.conversationsShownPerCoworker")}</span>
        <input
          type="number"
          min={1}
          max={50}
          value={peek}
          className="w-16 px-2 py-1.5 rounded-lg border border-line bg-paper text-ui text-ink outline-none focus:border-accent"
          onChange={(e) => save(Number(e.target.value))}
        />
      </label>
      <div className={FIELD_HELP}>
        {t("settings.sidebarHelp")}
      </div>
    </div>
  );
}

// -- Files (scratch location) — one card inside General (UX-021: a single option
// doesn't earn its own tab) -----------------------------------------------------
function FilesCard() {
  const { t } = useTranslation();
  const [settings, setSettings] = useState<ModelSettings | null>(null);
  const [scratchDraft, setScratchDraft] = useState("");
  const [scratchMsg, setScratchMsg] = useState<string | null>(null);
  const desktop = isTauri();

  const refresh = () =>
    getSettings()
      .then((s) => {
        setSettings(s);
        setScratchDraft((d) => d || s.scratch_base || "");
      })
      .catch(() => setSettings(null));
  useEffect(() => {
    refresh();
  }, []);

  const saveScratch = async () => {
    setScratchMsg(null);
    const res = await setScratchBase(scratchDraft.trim());
    if (res.ok) {
      setScratchMsg(t("settings.savedNewConversations"));
      refresh();
    } else {
      setScratchMsg(res.error || t("settings.couldNotUseLocation"));
    }
  };
  const browseScratch = async () => {
    const picked = await pickFolder();
    if (picked) setScratchDraft(picked);
  };

  if (!settings) return null;

  return (
    <div className={CARD + " p-4 mb-4"}>
      <div className={FIELD_LABEL}>{t("settings.files")}</div>
        <div className="flex items-center gap-2 mt-2.5">
          <input
            className={INPUT}
            type="text"
            placeholder={t("settings.filesPlaceholder")}
            value={scratchDraft}
            spellCheck={false}
            autoComplete="off"
            onChange={(e) => setScratchDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && saveScratch()}
          />
          {desktop && (
            <button className={BTN_BORDERED} onClick={browseScratch} title={t("settings.pickFolder")}>
              {t("settings.browse")}
            </button>
          )}
          <button className={BTN_ACCENT} onClick={saveScratch} disabled={!scratchDraft.trim()}>
            {t("settings.save")}
          </button>
        </div>
      <div className={FIELD_HELP}>
        {t("settings.filesHelp")}
      </div>
      {scratchMsg && <div className="text-[12.5px] text-muted mt-2.5">{scratchMsg}</div>}
    </div>
  );
}
