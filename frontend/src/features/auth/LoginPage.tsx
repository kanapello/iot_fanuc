import { useState } from "react";
import { api, setToken } from "../../api/client";
import { t } from "../../shared/i18n";

export function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    try {
      const result = await api<{ token: string }>("/auth/login", { method: "POST", body: { username, password } });
      setToken(result.token);
      window.location.assign(new URLSearchParams(window.location.search).get("next") ?? "/dashboard");
    } catch {
      setError(true);
    }
  };

  const input = "w-full rounded border border-slate-700 bg-slate-800 px-3 py-3 text-lg outline-none focus:border-sky-500";
  return (
    <div className="flex min-h-full items-center justify-center p-4">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-xl bg-slate-900 p-6 shadow-xl">
        <h1 className="text-2xl font-semibold">{t.app.title}</h1>
        <label className="block space-y-1">
          <span className="text-sm text-slate-400">{t.login.username}</span>
          <input className={input} value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
        </label>
        <label className="block space-y-1">
          <span className="text-sm text-slate-400">{t.login.password}</span>
          <input className={input} type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        {error && <p className="text-sm text-red-400">{t.login.failed}</p>}
        <button className="w-full rounded bg-sky-600 py-3 text-lg font-semibold hover:bg-sky-500">{t.login.submit}</button>
        <p className="text-xs text-slate-500">{t.login.hint}</p>
      </form>
    </div>
  );
}
