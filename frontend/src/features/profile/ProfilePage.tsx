import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, type NotificationType, type TelegramStatus } from "../../api/client";
import { t } from "../../shared/i18n";
import { useMe } from "../../shared/session";
import { formatMoment } from "../repairs/common";

const button = "min-h-[44px] rounded-lg px-4 text-sm font-medium disabled:opacity-50";

/** Telegram for notifications: link the chat by a one-time link, unlink it. */
function TelegramCard({ hasTypes }: { hasTypes: boolean }) {
  const client = useQueryClient();
  const [link, setLink] = useState<{ url: string; until: number } | null>(null);
  const [error, setError] = useState("");
  const waiting = link !== null && Date.now() < link.until;
  const status = useQuery({
    queryKey: ["profile-telegram"],
    queryFn: () => api<TelegramStatus>("/profile/telegram"),
    refetchInterval: waiting ? 3000 : false, // until the bot confirms the link
  });
  useEffect(() => {
    if (link && status.data?.linked) setLink(null);
  }, [link, status.data]);
  const data = status.data;

  const connect = async () => {
    setError("");
    try {
      const result = await api<{ url: string; expires_at: string }>("/profile/telegram/link", { method: "POST" });
      setLink({ url: result.url, until: Date.parse(result.expires_at) });
      await client.invalidateQueries({ queryKey: ["profile-telegram"] });
    } catch (e) {
      setError(e instanceof Error ? e.message : t.common.error);
    }
  };
  const disconnect = async () => {
    if (!window.confirm(t.profile.confirmDisconnect)) return;
    await api("/profile/telegram", { method: "DELETE" });
    setLink(null);
    await client.invalidateQueries({ queryKey: ["profile-telegram"] });
  };

  return (
    <section className="space-y-3 rounded-xl bg-slate-900 p-4">
      <h2 className="text-lg font-semibold">{t.profile.telegram}</h2>
      {!data ? (
        <p className="text-slate-400">{t.common.loading}</p>
      ) : !data.configured ? (
        <p className="text-slate-400">{t.profile.notConfigured}</p>
      ) : !data.linked && !hasTypes ? (
        <p className="text-slate-400">{t.profile.noEvents}</p>
      ) : data.linked ? (
        <div className="flex flex-wrap items-center gap-3">
          <span className="rounded bg-green-700 px-2 py-1 text-sm font-semibold text-white">{t.profile.linked}</span>
          <span className="text-slate-300">
            {data.username ? `@${data.username} · ` : ""}
            {t.profile.since} {formatMoment(data.linked_at)}
          </span>
          <button className={`${button} bg-slate-700 hover:bg-slate-600`} onClick={disconnect}>
            {t.profile.disconnect}
          </button>
        </div>
      ) : (
        <div className="space-y-3">
          <p className="text-slate-400">{t.profile.notLinked}</p>
          {waiting && link ? (
            <div className="space-y-2">
              <a
                className={`${button} inline-flex items-center bg-sky-600 text-white hover:bg-sky-500`}
                href={link.url}
                target="_blank"
                rel="noreferrer"
              >
                {t.profile.open} @{data.bot}
              </a>
              <p className="text-sm text-slate-400">{t.profile.connectHint}</p>
              <p className="text-sm text-amber-300">{t.profile.waiting}</p>
            </div>
          ) : (
            <button className={`${button} bg-sky-600 text-white hover:bg-sky-500`} onClick={connect}>
              {t.profile.connect}
            </button>
          )}
        </div>
      )}
      {error && <p className="text-sm text-red-300">{error}</p>}
    </section>
  );
}

const useEventTypes = () =>
  useQuery({ queryKey: ["profile-notifications"], queryFn: () => api<NotificationType[]>("/profile/notifications") });

/** Event types of the user's roles; each can be switched off. */
function EventTypesCard() {
  const client = useQueryClient();
  const types = useEventTypes();
  const [busy, setBusy] = useState("");
  const toggle = async (type: string, enabled: boolean) => {
    setBusy(type);
    try {
      const result = await api<NotificationType[]>("/profile/notifications", { method: "PUT", body: { enabled: { [type]: enabled } } });
      client.setQueryData(["profile-notifications"], result);
    } finally {
      setBusy("");
    }
  };
  return (
    <section className="space-y-3 rounded-xl bg-slate-900 p-4">
      <div>
        <h2 className="text-lg font-semibold">{t.profile.events}</h2>
        <p className="text-sm text-slate-400">{t.profile.eventsHint}</p>
      </div>
      {types.data && !types.data.length && <p className="text-slate-400">{t.profile.noEvents}</p>}
      <div className="space-y-1">
        {(types.data ?? []).map((item) => (
          <label key={item.type} className="flex min-h-[44px] cursor-pointer items-center gap-3 rounded-lg px-2 hover:bg-slate-800">
            <input
              type="checkbox"
              className="h-5 w-5"
              checked={item.enabled}
              disabled={busy === item.type}
              onChange={(e) => toggle(item.type, e.target.checked)}
            />
            <span>{t.profile.eventTypes[item.type] ?? item.type}</span>
          </label>
        ))}
      </div>
    </section>
  );
}

export function ProfilePage() {
  const { data: me } = useMe();
  const types = useEventTypes();
  if (!me) return <p className="text-slate-400">{t.common.loading}</p>;
  if (me.kind !== "user") return <p className="text-slate-400">{t.common.noAccess}</p>;
  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <h1 className="text-xl font-semibold">
        {t.profile.title}: {me.name}
      </h1>
      <TelegramCard hasTypes={Boolean(types.data?.length)} />
      {Boolean(types.data?.length) && <EventTypesCard />}
    </div>
  );
}
