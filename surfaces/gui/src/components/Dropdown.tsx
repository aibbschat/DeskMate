import { Fragment, useState } from "react";
import { useTranslation } from "react-i18next";
import { Icon } from "./Icon";

export interface Option {
  value: string;
  label: string;
  description?: string;
}

export interface OptionGroup {
  title: string;
  options: Option[];
  /** 档位徽标（2026-10-13 厂商分组）：free = 免费车道，paid = 需付费 key。 */
  tier?: "free" | "paid";
}

interface Props {
  prefix?: string;
  value: string;
  options?: Option[]; // flat list (used when `groups` is absent)
  groups?: OptionGroup[]; // grouped list (renders collapsible section headers)
  onChange: (value: string) => void;
  align?: "left" | "right";
  // Extra classes appended to the trigger pill (e.g. "chip" for a bordered composer-head chip).
  className?: string;
}

// 少于等于这个条数的组默认展开（小组折叠反而多一次点击）；更多的组默认收起，
// 含当前选中项的组永远默认展开（打开选择器先看到自己选的是什么）。
const AUTO_OPEN_MAX = 4;

export function Dropdown({ prefix, value, options, groups, onChange, align = "left", className }: Props) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  // 用户手动折叠/展开的覆盖（组内 mount 期间有效）。key = title + tier。
  const [overrides, setOverrides] = useState<Record<string, boolean>>({});
  const flat = groups ? groups.flatMap((g) => g.options) : (options || []);
  const current = flat.find((o) => o.value === value);
  const label = (prefix ? `${prefix}: ` : "") + (current?.label || value);

  const groupKey = (g: OptionGroup) => `${g.title}\u0000${g.tier ?? ""}`;
  const isOpen = (g: OptionGroup) => {
    const k = groupKey(g);
    if (k in overrides) return overrides[k];
    return g.options.length <= AUTO_OPEN_MAX || g.options.some((o) => o.value === value);
  };
  const toggle = (g: OptionGroup) =>
    setOverrides((prev) => ({ ...prev, [groupKey(g)]: !isOpen(g) }));

  const renderItem = (o: Option) => (
    <div
      key={o.value}
      className={"dd-item" + (o.value === value ? " sel" : "")}
      onClick={() => {
        onChange(o.value);
        setOpen(false);
      }}
    >
      <div className="dd-label">
        {o.label}
        {o.value === value && <span className="chk">✓</span>}
      </div>
      {o.description && <div className="dd-desc">{o.description}</div>}
    </div>
  );

  return (
    <div className="dd">
      <button
        className={"pill" + (className ? " " + className : "")}
        onClick={() => setOpen((v) => !v)}
        title={label}
      >
        <span className="pill-label">{label}</span>
        <Icon name="chevronDown" size={13} className="caret" />
      </button>
      {open && (
        <>
          <div className="dd-backdrop" onClick={() => setOpen(false)} />
          <div className={"dd-menu " + align}>
            {groups
              ? groups.map((g) => (
                  <Fragment key={groupKey(g)}>
                    <button
                      type="button"
                      className={"dd-group-head" + (isOpen(g) ? " open" : "")}
                      onClick={() => toggle(g)}
                      aria-expanded={isOpen(g)}
                    >
                      <Icon name="chevronRight" size={11} className="dd-group-caret" />
                      <span className="dd-group-name">{g.title}</span>
                      {g.tier && (
                        <span className={"dd-tier-badge " + g.tier}>
                          {g.tier === "free"
                            ? t("composer.freeTag")
                            : t("composer.paidTag")}
                        </span>
                      )}
                      <span className="dd-group-count">{g.options.length}</span>
                    </button>
                    {isOpen(g) && g.options.map(renderItem)}
                  </Fragment>
                ))
              : (options || []).map(renderItem)}
          </div>
        </>
      )}
    </div>
  );
}
