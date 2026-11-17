import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Machine, type Me } from "../../api/client";
import { t } from "../../shared/i18n";
import { inputClass } from "./NodeForm";

interface DeviceToken {
  id: number;
  name: string;
  machine_id: number | null;
  machine_name: string | null;
  area_id: number | null;
  area_name: string | null;
  created_at: string;
  revoked_at: string | null;
}

export function TokensPanel({ me }: { me: Me }) {
  const client = useQueryClient();
  const { data: tokens } = useQuery({ queryKey: ["device-tokens"], queryFn: () => api<DeviceToken[]>("/device-tokens") });
  const { data: machines } = useQuery({ queryKey: ["machines"], queryFn: () => api<Machine[]>("/machines") });
  const areas = me.sites.flatMap((s) => s.areas.map((a) => ({ ...a, label: `${s.name} → ${a.name}` })));
  const [kind, setKind] = useState<"machine" | "area">("machine");
  const [target, setTarget] = useState("");
  const [name, setName] = useState("");
  const [issued, setIssued] = useState("");
  const [error, setError] = useState("");

  const issue = async () => {
    setError("");
    try {
      const id = Number(target || (kind === "machine" ? machines?.[0]?.id : areas[0]?.id));
      const body = { name: name || (kind === "machine" ? t.tokens.machine : t.tokens.area), [kind === "machine" ? "machine_id" : "area_id"]: id };
      const result = await api<{ token: string }>("/device-tokens", { method: "POST", body });
      const page = kind === "machine" ? `/operator/${id}` : `/dashboard/${id}`;
      setIssued(`${window.location.origin}${page}?token=${encodeURIComponent(result.token)}`);
      setName("");
      await client.invalidateQueries({ queryKey: ["device-tokens"] });
    } catch (e) {
      setError(e instanceof Error ? e.message : t.common.error);
    }
  };
  const revoke = async (id: number) => {
    if (!window.confirm(t.tokens.confirmRevoke)) return;
    await api(`/device-tokens/${id}`, { method: "DELETE" });
    await client.invalidateQueries({ queryKey: ["device-tokens"] });
  };

  return (
    <div className="space-y-4">
      {error && <p className="rounded bg-red-900/60 px-3 py-2 text-sm text-red-200">{error}</p>}
      <div className="space-y-2 rounded-xl bg-slate-900 p-4">
        <h3 className="font-semibold">{t.tokens.issue}</h3>
        <div className="flex flex-wrap items-center gap-2">
          <input className={inputClass} placeholder={t.tokens.name} value={name} onChange={(e) => setName(e.target.value)} />
          <select className={inputClass} value={kind} onChange={(e) => { setKind(e.target.value as "machine" | "area"); setTarget(""); }}>
            <option value="machine">{t.tokens.machine}</option>
            <option value="area">{t.tokens.area}</option>
          </select>
          <select className={inputClass} value={target} onChange={(e) => setTarget(e.target.value)}>
            {kind === "machine"
              ? (machines ?? []).map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.site_name} → {m.name}
                  </option>
                ))
              : areas.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.label}
                  </option>
                ))}
          </select>
          <button className="rounded bg-sky-600 px-3 py-1 text-sm hover:bg-sky-500" onClick={issue}>
            {t.tokens.issue}
          </button>
        </div>
        {issued && (
          <div className="rounded bg-emerald-900/50 p-3 text-sm">
            <p>{t.tokens.issued}</p>
            <code className="mt-1 block break-all text-emerald-200">{issued}</code>
          </div>
        )}
      </div>
      <div className="rounded-xl bg-slate-900 p-4">
        <table className="w-full text-sm">
          <thead className="text-left text-slate-400">
            <tr>
              <th className="px-2 py-1">{t.tokens.name}</th>
              <th className="px-2 py-1">{t.tokens.target}</th>
              <th className="px-2 py-1">{t.tokens.created}</th>
              <th className="px-2 py-1" />
            </tr>
          </thead>
          <tbody>
            {(tokens ?? []).map((token) => (
              <tr key={token.id} className={`border-t border-slate-800 ${token.revoked_at ? "opacity-50" : ""}`}>
                <td className="px-2 py-1">{token.name}</td>
                <td className="px-2 py-1">{token.machine_name ? `${t.tokens.machine}: ${token.machine_name}` : `${t.tokens.area}: ${token.area_name}`}</td>
                <td className="px-2 py-1">{new Date(token.created_at).toLocaleString("ru-RU")}</td>
                <td className="px-2 py-1 text-right">
                  {token.revoked_at ? (
                    <span className="text-slate-400">{t.tokens.revoked}</span>
                  ) : (
                    <button className="rounded bg-slate-700 px-3 py-1 hover:bg-slate-600" onClick={() => revoke(token.id)}>
                      {t.tokens.revoke}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
