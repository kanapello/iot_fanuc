import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError, download } from "../../api/client";
import { t } from "../../shared/i18n";

export type Report = (message: string, isError?: boolean) => void;
type Row = Record<string, any>;

const button = "rounded bg-slate-700 px-3 py-1 text-sm hover:bg-slate-600 disabled:opacity-50";
const failure = (e: unknown) => (e instanceof Error && e.message ? `${t.common.error}: ${e.message}` : t.common.error);

/** The QR sticker of a machine (PDF) and a new code for it: the old sticker stops opening at once. */
export function MachinePassportActions({ row, report }: { row: Row; report: Report }) {
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const sticker = async () => {
    try {
      await download(`/admin/machines/${row.id}/passport.pdf`, `passport-${row.code}.pdf`);
    } catch (e) {
      report(failure(e), true);
    }
  };
  const reissue = async () => {
    if (!window.confirm(`${t.admin.passport.confirmReissue}${row.name}`)) return;
    setBusy(true);
    try {
      const result = await api<{ passport_code: string }>(`/admin/machines/${row.id}/passport`, { method: "POST" });
      report(`${t.admin.passport.reissued}${result.passport_code}`);
      await client.invalidateQueries({ queryKey: ["admin"] });
    } catch (e) {
      report(failure(e), true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <button className={button} onClick={sticker}>
        {t.admin.passport.sticker}
      </button>
      <button className={button} disabled={busy} onClick={reissue}>
        {t.admin.passport.reissue}
      </button>
    </>
  );
}

/** Stickers of every active machine of an area, ten to an A4 page. */
export function AreaStickersAction({ row, report }: { row: Row; report: Report }) {
  const sheet = async () => {
    try {
      // ASCII only: browsers may drop a Cyrillic name of a downloaded blob and save it as "download"
      await download(`/admin/areas/${row.id}/passports.pdf`, `passports-area-${row.id}.pdf`);
    } catch (e) {
      report(e instanceof ApiError && e.status === 422 ? t.admin.passport.emptyArea : failure(e), true);
    }
  };
  return (
    <button className={button} onClick={sheet}>
      {t.admin.passport.sheet}
    </button>
  );
}
