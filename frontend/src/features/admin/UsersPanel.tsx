import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Me } from "../../api/client";
import { t } from "../../shared/i18n";
import { nodeLabel, roleLabel } from "../../shared/session";
import { firstNode, inputClass, NodeForm, type NodeValue } from "./NodeForm";

interface Membership {
  id: number;
  role: string;
  org_id: number | null;
  site_id: number | null;
  area_id: number | null;
}

interface User {
  id: number;
  login: string;
  full_name: string;
  is_active: boolean;
  is_superadmin: boolean;
  memberships: Membership[];
}

const ORG_ROLES = ["operator", "master", "engineer", "admin"];
const button = "rounded bg-slate-700 px-3 py-1 text-sm hover:bg-slate-600";

function RoleForm({ me, onSubmit, submitLabel }: { me: Me; onSubmit: (role: string, node: NodeValue) => void; submitLabel: string }) {
  const [role, setRole] = useState("operator");
  const [node, setNode] = useState<NodeValue>(firstNode(me, false));
  const roles = me.superadmin ? [...ORG_ROLES, "service"] : ORG_ROLES;
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <select className={inputClass} value={role} onChange={(e) => setRole(e.target.value)}>
        {roles.map((r) => (
          <option key={r} value={r}>
            {roleLabel(r)}
          </option>
        ))}
      </select>
      {role === "service" ? (
        <span className="text-sm text-slate-400">{t.users.allOrganizations}</span>
      ) : (
        <NodeForm me={me} value={node} onChange={setNode} />
      )}
      <button
        className="rounded bg-sky-600 px-3 py-1 text-sm hover:bg-sky-500"
        onClick={() => onSubmit(role, role === "service" ? { org_id: null, site_id: null, area_id: null } : node)}
      >
        {submitLabel}
      </button>
    </span>
  );
}

export function UsersPanel({ me }: { me: Me }) {
  const client = useQueryClient();
  const { data: users } = useQuery({ queryKey: ["users"], queryFn: () => api<User[]>("/users") });
  const [error, setError] = useState("");
  const [draft, setDraft] = useState({ login: "", password: "", full_name: "" });

  const run = async (action: () => Promise<unknown>) => {
    setError("");
    try {
      await action();
      await client.invalidateQueries({ queryKey: ["users"] });
    } catch (e) {
      setError(e instanceof Error ? e.message : t.common.error);
    }
  };

  return (
    <div className="space-y-4">
      {error && <p className="rounded bg-red-900/60 px-3 py-2 text-sm text-red-200">{error}</p>}
      <div className="space-y-2 rounded-xl bg-slate-900 p-4">
        <h3 className="font-semibold">{t.users.create}</h3>
        <div className="flex flex-wrap items-center gap-2">
          <input className={inputClass} placeholder={t.users.login} value={draft.login} onChange={(e) => setDraft({ ...draft, login: e.target.value })} />
          <input className={inputClass} placeholder={t.users.password} type="password" value={draft.password} onChange={(e) => setDraft({ ...draft, password: e.target.value })} />
          <input className={inputClass} placeholder={t.users.fullName} value={draft.full_name} onChange={(e) => setDraft({ ...draft, full_name: e.target.value })} />
          <RoleForm
            me={me}
            submitLabel={t.users.create}
            onSubmit={(role, node) =>
              run(async () => {
                await api("/users", { method: "POST", body: { ...draft, membership: { role, ...node } } });
                setDraft({ login: "", password: "", full_name: "" });
              })
            }
          />
        </div>
      </div>
      <div className="rounded-xl bg-slate-900 p-4">
        <table className="w-full text-sm">
          <thead className="text-left text-slate-400">
            <tr>
              <th className="px-2 py-1">{t.users.login}</th>
              <th className="px-2 py-1">{t.users.fullName}</th>
              <th className="px-2 py-1">{t.users.roles}</th>
              <th className="px-2 py-1" />
            </tr>
          </thead>
          <tbody>
            {(users ?? []).map((user) => (
              <tr key={user.id} className={`border-t border-slate-800 align-top ${user.is_active ? "" : "opacity-50"}`}>
                <td className="px-2 py-2 font-medium">
                  {user.login}
                  {user.is_superadmin && <span className="ml-2 text-xs text-amber-400">{t.users.superadmin}</span>}
                </td>
                <td className="px-2 py-2">{user.full_name}</td>
                <td className="space-y-1 px-2 py-2">
                  {user.memberships.map((m) => (
                    <div key={m.id} className="flex items-center gap-2">
                      <span className="rounded bg-slate-700 px-2 py-0.5">{roleLabel(m.role)}</span>
                      <span className="text-slate-300">{nodeLabel(me, m.org_id, m.site_id, m.area_id)}</span>
                      <button
                        className="text-xs text-red-300 hover:underline"
                        onClick={() => window.confirm(t.users.confirmRemoveRole) && run(() => api(`/memberships/${m.id}`, { method: "DELETE" }))}
                      >
                        {t.users.removeRole}
                      </button>
                    </div>
                  ))}
                  {!user.is_superadmin && (
                    <RoleForm
                      me={me}
                      submitLabel={t.users.addRole}
                      onSubmit={(role, node) => run(() => api(`/users/${user.id}/memberships`, { method: "POST", body: { role, ...node } }))}
                    />
                  )}
                </td>
                <td className="whitespace-nowrap px-2 py-2 text-right">
                  {!user.is_superadmin && (
                    <span className="space-x-2">
                      <button
                        className={button}
                        onClick={() => {
                          const password = window.prompt(t.users.newPassword);
                          if (password) run(() => api(`/users/${user.id}`, { method: "PUT", body: { password } }));
                        }}
                      >
                        {t.users.setPassword}
                      </button>
                      <button className={button} onClick={() => run(() => api(`/users/${user.id}`, { method: "PUT", body: { is_active: !user.is_active } }))}>
                        {user.is_active ? t.users.deactivate : t.users.activate}
                      </button>
                    </span>
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
