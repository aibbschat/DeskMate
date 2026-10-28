import { useState } from "react";
import type { SkillEntry } from "../../api";
import { configureSkill } from "../../api";

export function ConfigModal({
  skill,
  onClose,
  onConfigured,
}: {
  skill: SkillEntry;
  onClose: () => void;
  onConfigured: () => void;
}) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSave = async () => {
    if (!key.trim()) {
      setError("请填写密钥");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const config: Record<string, string> = {};
      if (skill.config_env_key) {
        config[skill.config_env_key] = key.trim();
      }
      const res = await configureSkill(skill.id, config);
      if (res.ok) {
        onConfigured();
        onClose();
      } else {
        setError(res.error || "配置失败");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "配置失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="fixed inset-0 bg-black/40" onClick={onClose} />
      <div className="relative bg-panel rounded-xl border border-line shadow-2xl w-[380px] p-5">
        <div className="text-title font-semibold mb-1">
          配置 {skill.name}
        </div>
        <div className="text-meta text-muted mb-4">
          {skill.description}
        </div>

        <div className="mb-4">
          <label className="block text-ui font-medium mb-1.5">
            {skill.config_key_name || "API Key"}
          </label>
          <input
            type="password"
            className="w-full px-3 py-2 rounded-lg border border-line bg-panel text-ui outline-none focus:border-accent"
            placeholder={skill.config_key_hint || "输入密钥"}
            value={key}
            onChange={(e) => {
              setKey(e.target.value);
              if (error) setError(null);
            }}
            autoFocus
          />
          {skill.config_key_hint && (
            <div className="text-label text-faint mt-1">{skill.config_key_hint}</div>
          )}
        </div>

        {error && <div className="text-meta text-danger mb-3">{error}</div>}

        <div className="flex items-center justify-end gap-2">
          <button
            className="px-3 py-1.5 rounded-lg text-ui text-muted hover:bg-paper"
            onClick={onClose}
          >
            取消
          </button>
          <button
            className="px-4 py-1.5 rounded-lg bg-accent text-white text-ui font-medium hover:opacity-90 disabled:opacity-50"
            onClick={handleSave}
            disabled={busy}
          >
            {busy ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}
