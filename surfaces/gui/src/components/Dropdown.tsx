import { Fragment, useState } from "react";
import { Icon } from "./Icon";

export interface Option {
  value: string;
  label: string;
  description?: string;
}

export interface OptionGroup {
  title: string;
  options: Option[];
}

interface Props {
  prefix?: string;
  value: string;
  options?: Option[]; // flat list (used when `groups` is absent)
  groups?: OptionGroup[]; // grouped list (renders section titles)
  onChange: (value: string) => void;
  align?: "left" | "right";
  // Extra classes appended to the trigger pill (e.g. "chip" for a bordered composer-head chip).
  className?: string;
}

export function Dropdown({ prefix, value, options, groups, onChange, align = "left", className }: Props) {
  const [open, setOpen] = useState(false);
  const flat = groups ? groups.flatMap((g) => g.options) : (options || []);
  const current = flat.find((o) => o.value === value);
  const label = (prefix ? `${prefix}: ` : "") + (current?.label || value);

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
                  <Fragment key={g.title}>
                    <div className="dd-group-title">{g.title}</div>
                    {g.options.map(renderItem)}
                  </Fragment>
                ))
              : (options || []).map(renderItem)}
          </div>
        </>
      )}
    </div>
  );
}
