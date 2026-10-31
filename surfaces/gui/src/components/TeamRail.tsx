// Ported from upstream TeamRail.tsx — this fork only needs the roster search box
// (the rail itself is not part of this fork's layout), so the rest of that file is
// deliberately not carried over. Kept in its own module so TeamView's import matches
// upstream and a future rail port is a drop-in.

import { useTranslation } from "react-i18next";
import { Icon } from "./Icon";

export function WorkerSearch({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <label className="team-worker-search">
      <Icon name="search" size={15} />
      <input
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label={t("teamview.search_workers_label")}
        placeholder={t("teamview.search_workers")}
      />
    </label>
  );
}
